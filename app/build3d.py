# build3d.py
import copy
from logging import getLogger
import os
import sys
from typing import List, NoReturn, Optional, Union

import numpy as np
import open3d as o3d
from PIL import Image
import scipy
import shapely

from app.database import db
from app.pointcloud import crop_las
from app.zukaku import get_codes_in_area

logger = getLogger(__name__)


class Build3d(object):

    BUFFER = 1.0     # 切り出す際のバッファ（メートル）
    GRIDSIZE = 0.01  # 最高解像度のグリッドサイズ（メートル）
    LIMIT_POINTS = 500000  # 三次元点群の最大点数（0以下で無制限）

    def __init__(
            self,
            bldid: str,
            system_code: int,
            lod: int = 1,
            dirname: Optional[os.PathLike] = None):
        """
        指定した建物オブジェクトで初期化する。

        Parameters
        ----------
        bldid: str
            Plateau 建物ID (ex. '22203-bldg-97124')
        system_code: int
            平面直角座標系の系番号
            https://www.gsi.go.jp/sokuchikijun/jpc.html
        lod: int
            LOD を指定、デフォルトは 1
        dirname: PathLike, optional
            ファイルを出力するディレクトリのパス
            self.set_dirname() で後から指定しても良い
            指定しない場合は None になり、ファイル出力時例外になる
        """
        if system_code < 0 or system_code > 19:
            raise ValueError("System_code は 1 から 19 です。")

        # 初期設定
        self.bldid = bldid
        self.system_code = system_code
        self.lod = lod
        self.crs = "EPSG:{}".format(6668 + system_code)
        if dirname is not None:
            self.set_dirname(dirname)

        # 計算して取得する値
        self.building = None  # 建物オブジェクト: GeoDataFrame
        self.pcd = None       # 三次元点群: PointCloud
        self.distance_matrix = None  # 点と各面との距離
        self.gridsize = self.GRIDSIZE  # 三次元点群のグリッドサイズ

    def set_dirname(self, dirname: os.PathLike) -> bool:
        """
        ファイルを出力するディレクトリ名を指定する。

        Parameters
        ----------
        dirname: os.PathLike
            ディレクトリのパス

        Returns
        -------
        bool
            成功した場合は True

        Notes
        -----
        ディレクトリが存在しない場合は作成する。
        """
        # ディレクトリを作成
        try:
            os.makedirs(dirname, mode=0o755, exist_ok=True)
        except RuntimeError:
            return False

        self.dirname = dirname
        return True

    def get_building(self):
        """
        建物の geopandas データを DB から取得する。

        returns
        -------
        geopandas.geodataframe.GeoDataFrame
            対象となる建物オブジェクト。
            座標系は EPSG:6676
        """
        if self.building is not None:
            return self.building

        logger.info("Plateau建物を PostGIS から取得開始")
        exploded = db.get_plateau_building(
            bldid=self.bldid, lod=self.lod)  # CRS: 4326
        if exploded is None:
            raise RuntimeError(
                "Id='{}', lod={} のPlateau建物が見つかりません".format(
                    self.bldid, self.lod))

        # building は geopandas.geodataframe.GeoDataFrame オブジェクト
        self.building = exploded.to_crs(self.crs)  # 座標系変換
        logger.info("Plateau建物を PostGIS から取得完了")

        return self.building

    def make_objfiles(
            self,
            imagesize: Optional[int] = None,
            texture_mapping_method: str = 'smart') -> NoReturn:
        """
        建物オブジェクトの OBJ データを生成し、
        OBJ + MTL ファイルに出力する。

        Paramters
        ---------
        imagesize: int, optional
            テクスチャ画像の長辺の長さ
        texture_mapping_method: str
            テクスチャ画像を作成する手法
            - all: 全ての点を正射影
            - nearest: 各面にもっとも近い点をマッピング
            - smart: 最大深さを自動検出して正射影（デフォルト）

        Notes
        -----
        対象となる建物オブジェクトは self.building,
        その建物の id は self.bldid から取得する。
        """
        building = self.get_building()
        nfaces = len(building)
        faces = [building.iloc[i].geom for i in range(nfaces)]
        texture_images = [None] * nfaces
        prefix = "{}_lod{}_{}_{}_{}".format(
            self.bldid, self.lod,
            texture_mapping_method, imagesize,
            len(self.get_pointcloud().points))

        # self.write_pointcloud()

        # face[i] は building の i 番目の面
        # shapely.geometry.polygon.Polygon オブジェクト
        # 1 番目の面の 0..1 の辺と 0..-1 の辺を取得する
        face_vertices = []
        all_vertices = []
        surfaces = []
        for i, face in enumerate(faces):
            ring = face.exterior.coords
            logger.debug("面{}には{}個の頂点があります".format(
                i, len(ring)))
            vertices = []
            for v in ring[:-1]:
                try:
                    pos = all_vertices.index(v)
                except ValueError:
                    pos = len(all_vertices)
                    all_vertices.append(v)

                vertices.append(pos)

            face_vertices.append(vertices)

            # 面に投影する行列を計算
            surface = Surface(i, build3d=self)
            surfaces.append(surface)

        if texture_mapping_method.lower() == "all":
            """
            点群の全ての点を各面に正射投影する。
            背面の点も正面にマッピングされる。
            """
            logger.info("テクスチャ画像作成開始（method:all）")
            for n, surface in enumerate(surfaces):
                mask = True
                texture_images[n] = surface.create_texture_image(
                    prefix=prefix, mask=mask, imagesize=imagesize,
                    write_pointcloud=False)

        else:
            # 各面と点群の距離
            logger.info("点群と各面({})との距離を計算".format(nfaces))
            list_of_distances = []
            k = 0  # distance_matrix の行数
            nkmap = []  # 面n の距離リストが含まれる行番号
            for n, surface in enumerate(surfaces):
                distances = surface.get_distance_matrix(check_bounds=True)
                if len(distances) == 0:
                    # この建物に含まれる点群データがない場合
                    nkmap.append(None)
                    continue

                if self.lod == 1 and (n == 0 or n == len(surfaces) - 1):
                    # lod = 1 の時, 上面と底面には投影しない
                    distances = distances + 999.9

                min_dist = distances.min()
                logger.debug("- {}/{}, min_dist={:.3f}".format(
                    n + 1, nfaces, min_dist))
                if min_dist > 10.0:
                    # 面n に最も近い点が 10m 離れているので
                    # マッピング対象外とする
                    nkmap.append(None)
                    continue

                list_of_distances.append(distances)

                nkmap.append(k)
                k += 1

            distance_matrix = np.array(
                list_of_distances, dtype=np.float32)
            del list_of_distances  # メモリ解放

            # 最寄りの面を取得
            logger.info("最寄りの面に割り当て")
            if len(distance_matrix) == 0:
                nearest_face = False
            else:
                nearest_face = np.argmin(distance_matrix, axis=0)

            if texture_mapping_method.lower() == 'nearest':
                """
                各面を最も近い面とする点をマッピングの対象とする。
                """
                logger.info("テクスチャ画像作成開始（method:nearest）")
                for n, surface in enumerate(surfaces):
                    # 面nが最寄りで、かつ距離が999m以下
                    k = nkmap[n]
                    if k is None:
                        mask = False
                    else:
                        mask = (nearest_face == k) & (distance_matrix[k, :] < 999.0)

                    texture_images[n] = surface.create_texture_image(
                        prefix=prefix, mask=mask, imagesize=imagesize)

            else:
                """
                各面を最も近い面とする点のうち、建物内部にあり、
                距離がもっとも遠い点と、面の距離をしきい値として
                それより近い点をマッピングの対象とする。
                ただし、しきい値の上限は 10m とする。
                """
                logger.info("テクスチャ画像作成開始（method:smart）")
                for n, surface in enumerate(surfaces):
                    k = nkmap[n]
                    mask = (nearest_face == k)
                    projected_pcd = surface.get_projected_points()
                    filtered_pcd = projected_pcd[mask]
                    if len(filtered_pcd) == 0 or len(filtered_pcd[:, 2]) == 0:
                        mask = False
                    else:
                        # filtered_points[:, 2] は壁面からの距離（内部は負）
                        z = filtered_pcd[:, 2]
                        z_range = (min(-1.0, max(-10.0, z.min())),
                                   max(1.0, z.max()))
                        z = projected_pcd[:, 2]
                        mask = (z >= z_range[0]) & (z <= z_range[1])

                    texture_images[n] = surface.create_texture_image(
                        prefix=prefix, mask=mask, imagesize=imagesize)

        logger.info("テクスチャ画像作成完了")

        # OBJ ファイル出力
        # https://en.wikipedia.org/wiki/Wavefront_.obj_file
        objfilename = os.path.join(self.dirname, "{}.obj".format(prefix))
        with open(objfilename, 'w') as f:
            print("mtllib {}.mtl".format(prefix), file=f)
            print("o {}".format(self.bldid), file=f)

            # 頂点座標列を出力
            for vertice in all_vertices:
                print("v {:.4f} {:.4f} {:.4f}".format(
                    vertice[0], vertice[1], vertice[2]),
                    file=f)

            # 法線ベクトルを出力しつつテクスチャ座標を計算
            vt_list = []
            vt_index = []
            for n, surface in enumerate(surfaces):
                nv = surface.projection_matrix[:, 2]
                print("vn {:.4f} {:.4f} {:.4f}".format(
                    nv[0], nv[1], nv[2]), file=f)

                # テクスチャ座標を計算
                minx, miny, maxx, maxy = surface.boundary
                width, height = maxx - minx, maxy - miny
                face_vt_index = []
                for i, vertice in enumerate(
                        surface.projected_vertices):
                    texture_coord = (
                        round((vertice[0] - minx) / width, 3),
                        round(1.0 - (vertice[1] - miny) / height, 3))
                    try:
                        pos = vt_list.index(texture_coord)
                    except ValueError:
                        pos = len(vt_list)
                        vt_list.append(texture_coord)

                    face_vt_index.append(pos)

                vt_index.append(face_vt_index)

            # テクスチャ座標を出力
            for tx, ty in vt_list:
                print("vt {:.3f} {:.3f}".format(tx, ty), file=f)

            # 面の列を出力
            for n, vertices in enumerate(face_vertices):
                print("usemtl {}".format(texture_images[n]), file=f)
                values = []
                for i, v in enumerate(vertices):
                    values.append("{}/{}/{}".format(
                        # 立体中の頂点, テクスチャ座標, 法線
                        v + 1, vt_index[n][i] + 1, n + 1))

                print("f {}".format(" ".join(values)),
                      file=f)

            logger.info("OBJ ファイル '{}' を出力完了".format(
                os.path.basename(objfilename)))

        # MTL ファイル出力
        # 面ごとにマテリアルとテクスチャを指定
        mtlfilename = os.path.join(
            self.dirname, '{}.mtl'.format(prefix))
        with open(mtlfilename, 'w') as f:
            for n in range(nfaces):
                texture_image = texture_images[n]
                print("newmtl {}".format(texture_image), file=f)
                print("Kd 1 1 1\nNs 0\nd 1\nillum 1\nKa 0 0 0\nKs 1 1 1",
                      file=f)
                print("map_Kd {}".format(texture_image), file=f)

            logger.info("MTL ファイル '{}' を出力完了".format(
                os.path.basename(mtlfilename)))

    def get_pointcloud(
            self,
            level: int = 50,
            limit_points: Optional[int] = None,
            lasfiles: Optional[List[os.PathLike]] = None):
        """
        建物領域に粗く一致する三次元点群をファイルから読み込む。

        Paramters
        ---------
        level: int, optional
            点群ファイルを分割した地図情報レベル、省略時は 50。
        limit_points: int, optional
            保持する最大の点数。省略時は LIMIT_POINTS に従う。
            読み込んだ点数がこの値を超える場合、ダウンサンプリングする。
            0 または負の値を指定した場合は、ダウンサンプリングしない。
        lasfiles: List[PathLike], optional
            読み込む LAS ファイルのリスト。
            省略した場合、 `./data` の下から探す。

        Returns
        -------
        o3d.geometry.PointCloud
            切り出した三次元点群

        Notes
        -----
        三次元点群ファイル (*.las) は環境変数 `LASDIR` が指すディレクトリ
        または未設定の場合は `./data` に配置する。
        対象となる LAS ファイル名を計算するため、系番号と地図情報レベルが
        必要となるため、
        """
        if self.pcd is not None:
            return self.pcd

        if limit_points is None:
            limit_points = self.LIMIT_POINTS

        building = self.get_building()
        boundary = building.total_bounds  # [minx, miny, maxx, maxy]
        boundary = [
            boundary[0] - self.BUFFER,
            boundary[1] - self.BUFFER,
            boundary[2] + self.BUFFER,
            boundary[3] + self.BUFFER
        ]

        if lasfiles is None:
            codes = get_codes_in_area(
                boundary[0], boundary[1], boundary[2], boundary[3],
                self.system_code, level
            )
            data_dir = os.environ.get('LASDIR', './data')
            lasfiles = [os.path.join(data_dir, code + '.las') for code in codes]

        logger.info("LAS データを読み込み開始")
        try:
            pcd = crop_las(boundary, lasfiles)
        except RuntimeError as e:
            logger.error("LAS が読み込めませんでした")
            raise RuntimeError(e)

        # 三次元点群を建物ポリゴンで切り取り
        # 上面と底面を切り出すのに有効
        # pcd = crop_point_cloud(pcd, building, buffer_size=self.BUFFER)
        pcd = self.crop_point_cloud(pcd)
        logger.info("三次元点群を建物ポリゴンで切り取り完了")

        # データ量を減らすためダウンサンプリング
        down_pcd = copy.copy(pcd)
        gridsize = self.GRIDSIZE
        while limit_points > 0 and len(down_pcd.points) > limit_points:
            down_pcd = pcd.voxel_down_sample(
                voxel_size=gridsize)
            logger.debug("gridsize:{:.02f} でダウンサンプリング ({})".format(
                gridsize, len(down_pcd.points)))
            self.gridsize = gridsize
            gridsize *= 1.41421356

        self.pcd = down_pcd
        return self.pcd

    def write_pointcloud(self):
        """
        三次元点群を LAS ファイルに出力する。
        """
        pcd = self.get_pointcloud()

        os.makedirs(self.dirname, mode=0o755, exist_ok=True)
        plyfilename = os.path.join(
            self.dirname, '{}.ply'.format(self.bldid))
        o3d.io.write_point_cloud(plyfilename, pcd)
        logger.info(
            "PLY ファイル '{}' を出力完了".format(plyfilename))

    def crop_point_cloud(self, pcd):
        """
        三次元点群を建物床面を垂直に伸ばした柱で切り取る。

        Parameters
        ----------
        pcd: o3d.geometry.PointCloud
            入力となる三次元点群

        Returns
        -------
        o3d.geometry.PointCloud
            切り取った三次元点群
        """
        plateau = db.get_plateau_building_2d(
            bldid=self.bldid, lod=self.lod)
        floor = plateau.geom.to_crs(self.crs)
        boundary = floor.buffer(self.BUFFER)

        # 高さ 300 の柱状ボリュームを作成
        select_vol = o3d.visualization.SelectionPolygonVolume()
        select_vol.orthogonal_axis = "z"
        select_vol.axis_min = 0
        select_vol.axis_max = 300
        bldg_ext_pts = list(boundary.exterior[0].coords)
        polygon = np.array(bldg_ext_pts, dtype=np.float32)
        polygon = np.insert(polygon, 2, 0, axis=1)
        select_vol.bounding_polygon = o3d.utility.Vector3dVector(polygon)

        # Crop
        return select_vol.crop_point_cloud(pcd)

    def count_points_near_walls(self, threshold: float = 1.0) -> int:
        """
        壁面のそばの点の数をカウントする

        Parameters
        ----------
        threshold: float
            壁面のそばと判定する距離のしきい値

        Returns
        -------
        int
            点群に含まれる点の数
        """
        if self.pcd is None:
            self.get_pointcloud()

        if len(self.pcd.points) == 0:
            return 0

        building = self.get_building()
        nfaces = len(building)

        # 壁面のリストと面積を取得
        surfaces = []
        total_area = 0.0
        for i in range(nfaces):
            surface = Surface(i, build3d=self)
            total_area += surface.area
            surfaces.append(surface)

        # 各壁面と点群の距離を計算
        list_of_distances = []
        for n, surface in enumerate(surfaces):
            distances = surface.get_distance_matrix(check_bounds=True)
            if self.lod == 1 and (n == 0 or n == len(surfaces) - 1):
                # lod = 1 の時, 上面と底面には投影しない
                distances = distances + 999.9

            min_dist = distances.min()
            logger.debug("- {}/{}, min_dist={:.3f}".format(
                n + 1, nfaces, min_dist))
            if min_dist > threshold:
                # 面n に最も近い点がしきい値より離れているので
                # マッピング対象外とする
                continue

            list_of_distances.append(distances)

        if len(list_of_distances) == 0:
            return 0

        distance_matrix = np.array(
            list_of_distances, dtype=np.float32)
        del list_of_distances  # メモリ解放

        # 最寄りの面との距離を取得
        dists = distance_matrix.min(axis=0)
        count = (dists[:] <= threshold).sum()

        return int(count)

    def get_surface_area(self) -> float:
        """
        壁面の面積の総和を求める

        Returns
        -------
        float
            面積（平方メートル）
        """
        building = self.get_building()
        nfaces = len(building)

        # 壁面のリストと面積を取得
        total_area = 0.0
        for i in range(nfaces):
            surface = Surface(i, build3d=self)
            total_area += surface.area

        return total_area


class Surface(object):
    """
    面の情報を管理するクラス
    """

    DEFAULT_IMAGESIZE = 1024  # テクスチャ画像の規定長辺ピクセル数

    def __init__(
            self,
            face_number: int,
            build3d: Build3d):
        """
        初期化する。

        Parameters
        ----------
        face_number: int
            建物オブジェクトの何番目の面かを表す数字（0 開始）
        build3d: Build3d
            この面を含む建物オブジェクト
        """
        building = build3d.get_building()
        # この面を構成する shapely.geometry.polygon.Polygon
        self.face_number = face_number
        self.face = building.iloc[face_number].geom
        self.build3d = build3d

        # 計算によって得られる属性
        self.origin = None    # 原点座標（building の座標系）
        self.boundary = None  # X,Y 境界（射影後の座標系）
        self.area = None      # 面積（射影後の座標系）
        self.projected_vertices = None   # 頂点列（射影後の座標系）
        self.projection_matrix = None    # 射影行列
        self.calc_basic_metrics()

    def calc_basic_metrics(self) -> NoReturn:
        """
        基本的な属性を事前に計算する。
        """
        ring = self.face.exterior.coords
        vertices = [v for v in ring[:-1]]
        vertices = np.array(vertices, dtype=np.float64)
        self.origin = vertices[0].copy()   # 最初の点を原点とする
        vertices = vertices - self.origin  # 頂点を移動

        v0 = vertices[1] - vertices[0]  # これを基線とする
        v1 = vertices[-1] - vertices[0]  # これを面を構成する線とする

        # 面の法線ベクトルを計算
        # 単位ベクトルではない
        v2 = np.cross(v0, v1)

        # v1 は v0 と直行しているとは限らないので、
        # v0, v2 と直行し、v0-v1 平面上のベクトルを再計算
        # これも単位ベクトルではない
        v1 = np.cross(v2, v0)

        # それぞれの単位ベクトルを求める
        u0 = v0 / np.linalg.norm(v0)
        u1 = v1 / np.linalg.norm(v1)
        u2 = v2 / np.linalg.norm(v2)

        # 平面 ax + by + cz + d = 0 のパラメータ（切片）d を求める
        # v0 を原点としているので、v0-v1 平面は原点を通る
        # -> d = 0
        # d = - np.dot(u2, vertices[0])

        # 点群を v0-v1 平面に投影する行列
        self.projection_matrix = np.array([
            (u0[0], u1[0], u2[0]),
            (u0[1], u1[1], u2[1]),
            (u0[2], u1[2], u2[2])],
            dtype=np.float64)

        # 面の各頂点を v0-v1 平面に投影し、 X, Y の最小・最大を計算
        self.projected_vertices = np.float32(
            np.matmul(
                vertices, self.projection_matrix))
        try:
            self.area = shapely.geometry.Polygon(
                self.projected_vertices[:, 0:2]).area
        except ValueError:
            raise RuntimeError("Wall vertices are overlapping.")
            self.area = 0.0

        x = self.projected_vertices[:, 0]
        y = self.projected_vertices[:, 1]
        minx, maxx = x.min(), x.max()
        miny, maxy = y.min(), y.max()
        self.boundary = [minx, miny, maxx, maxy]

    def get_distance_matrix(self, check_bounds: bool = False):
        """
        面と点群の距離 [d] を作成する。

        Parameters
        ----------
        check_bounds: bool
            正射投影時に面の範囲外になる点に対して、
            ペナルティとして距離 999.9 を加算する。

        Returns
        -------
        numpy.ndarray(npoints, 1)
            面との距離（絶対値）のベクトル。
        """
        minx, miny, maxx, maxy = self.boundary
        projected_points = self.get_projected_points()
        x = projected_points[:, 0]
        y = projected_points[:, 1]
        z = projected_points[:, 2]
        outbound_mask = (x < minx) | (x > maxx) | (y < miny) | (y > maxy)
        distances = np.fabs(z, dtype=np.float32) + outbound_mask * 999.9
        del projected_points
        return distances

    def get_projected_points(self):
        """
        三次元点群を v0 - v1 平面に投影した点群を計算する。
        """
        pcd = self.build3d.get_pointcloud()
        # 三次元点群を v0-v1 平面に投影
        points = np.asarray(pcd.points)
        points = points - self.origin  # v[0] を原点に移動
        projected_points = np.matmul(
            points,
            self.projection_matrix,
            dtype=np.float32)  # v0-v1 平面に投影

        return projected_points

    def create_texture_image(
            self,
            mask: Union[np.ndarray, bool] = True,
            imagesize: Optional[int] = None,
            prefix: Optional[str] = None,
            write_pointcloud: Optional[bool] = False) -> str:
        """
        面の PLY データとテクスチャ PNG 画像を出力する。

        Parameters
        ----------
        mask: numpy.ndarray, optional
            この面に出力する要素が True の ndarray
            すべての要素を出力する場合は True
        imagesize: int
            出力するテクスチャ画像の長辺のピクセル数
            省略した場合は DEFAULT_IMAGESIZE
        prefix: str
            出力するファイルの prefix
            省略した場合は buildid
        write_pointcloud: bool
            LAS ファイルを出力かするかどうかのフラグ
            デフォルトは False

        Returns
        -------
        str
            テクスチャ画像のファイル名（basename）

        Notes
        -----
        テクスチャ画像は上下反転していることに注意。
        """
        if imagesize is None:
            imagesize = self.DEFAULT_IMAGESIZE

        minx, miny, maxx, maxy = self.boundary
        bldid = self.build3d.bldid
        prefix = prefix or bldid

        # ピクセル当たりのサイズを計算
        gridsize = max(
            (maxx - minx) / (imagesize - 1.0),
            (maxy - miny) / (imagesize - 1.0),
            self.build3d.gridsize)

        # v0-v1 平面に投影した三次元点群（全て）
        projected_points = self.get_projected_points()

        # mask で選択された点のうち、X, Y が面の範囲内にある点を抽出
        x = projected_points[:, 0]
        y = projected_points[:, 1]
        z = projected_points[:, 2]  # noqa F841
        mask = mask & (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
        if not np.any(mask):  # mask.sum() < 10000
            logger.debug("面{}には条件を満たす点がありません".format(
                self.face_number))
            image = Image.new('RGB', (4, 4), (128, 128, 128))
            self.pngfilename = os.path.join(
                self.build3d.dirname, 'no_texture.png')
            if not os.path.exists(self.pngfilename):
                image.save(self.pngfilename)

            return os.path.basename(self.pngfilename)

        pcd = self.build3d.get_pointcloud()
        filtered_points = projected_points[mask]
        filtered_colors = np.asarray(pcd.colors)[mask]

        # 点群を PLY ファイルに出力
        new_pcd = o3d.geometry.PointCloud()
        new_pcd.points = o3d.utility.Vector3dVector(filtered_points)
        new_pcd.colors = o3d.utility.Vector3dVector(filtered_colors)
        if gridsize > self.build3d.gridsize * 2.0:
            # データ量を減らすためダウンサンプリング
            down_pcd = new_pcd.voxel_down_sample(
                voxel_size=gridsize)
            logger.debug("ダウンサンプリング {} -> {}".format(
                len(new_pcd.points), len(down_pcd.points)))
            new_pcd = down_pcd

        if write_pointcloud:
            self.plyfilename = os.path.join(
                self.build3d.dirname,
                '{}_{:03d}.ply'.format(prefix, self.face_number))
            o3d.io.write_point_cloud(self.plyfilename, new_pcd)
            logger.info("面{}の点群ファイルを '{}' に出力完了".format(
                self.face_number, self.plyfilename))

        # 画像を作成
        # [x, y, v] のリストから [[v]] の行列を作成する
        # https://qiita.com/shu32/items/f19635eab402ea6fc44e
        xyarray = filtered_points[:, 0:2]
        width = int((maxx - minx) / gridsize) + 1
        height = int((maxy - miny) / gridsize) + 1
        new_xcoord = np.linspace(minx, maxx, width)
        new_ycoord = np.linspace(miny, maxy, height)
        xx, yy = np.meshgrid(new_xcoord, new_ycoord)
        rgbarray = (scipy.interpolate.griddata(
            xyarray,
            filtered_colors[:, :],
            (xx, yy),
            method='nearest') * 256).astype(np.uint8)

        # このままだと点群が大きく欠損している部分も全て埋めてしまうので
        # 補完するのは gridsize * 2 までに制限し、それ以上離れた部分は
        # (128, 128, 128) で塗りつぶす
        tree = scipy.spatial.cKDTree(xyarray)
        xi = scipy.interpolate.interpnd._ndim_coords_from_arrays((xx, yy))
        dists, indexes = tree.query(xi)
        rgbarray[dists > gridsize * 2] = 128

        # PNG ファイルに出力
        image = Image.fromarray(rgbarray)
        self.pngfilename = os.path.join(
            self.build3d.dirname,
            '{}_{:03d}.png'.format(prefix, self.face_number))
        image.save(self.pngfilename)
        basename = os.path.basename(self.pngfilename)
        logger.info("面{}のテクスチャ画像を '{}' に出力完了".format(
            self.face_number, basename))

        return os.path.basename(basename)


if __name__ == '__main__':
    """
    Usage: python test3d.py [Plateau建物ID]

    省略した場合は '22203-bldg-97124'
    """
    if len(sys.argv) > 1:
        bldid = sys.argv[1]
    else:
        bldid = '22203-bldg-97124'

    logger.info("建物 '{}' の3Dモデルを作成します".format(bldid))
    build3d = Build3d(bldid=bldid, system_code=8, dirname=bldid)
    build3d.write_pointcloud()
    build3d.make_objfiles()
