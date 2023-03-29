import logging
import os
from typing import List

import laspy
import numpy as np
import open3d as o3d

logger = logging.getLogger(__name__)


def read_lasfiles(
        boundary: [float, float, float, float],
        lasfiles: List[os.PathLike]):
    """
    Read LAS data from files.

    Parameters
    ----------
    boundary: List[float, float, float, float]
        Boundary values of the area to be cropped.
        Must be a list in the following format;
        [minx, miny, maxx, maxy]
    lasfiles: List[PathLike]
        Name of target LAS files.

    see: https://laspy.readthedocs.io/en/latest/examples.html
    """
    point_stack = None
    for filename in lasfiles:
        if not os.path.exists(filename):
            logger.warning(
                f"File '{filename}' is skipped since it doesn't exist.")
            continue
        else:
            logger.debug("Reading '{}'".format(filename))

        with laspy.open(filename) as f:
            # scale: f.header.scale, offset: f.header.offset
            for points in f.chunk_iterator(10000):
                x, y = points.x.copy(), points.y.copy()  # For performance
                mask = (x >= boundary[0]) & (x <= boundary[2]) & \
                    (y >= boundary[1]) & (y <= boundary[3])

                if not np.any(mask):
                    continue

                new_array = np.stack((
                    x[mask],
                    y[mask],
                    points.z.copy()[mask],
                    points.intensity.copy()[mask],
                    points.red.copy()[mask] / 65536.0,
                    points.green.copy()[mask] / 65536.0,
                    points.blue.copy()[mask] / 65536.0),
                    axis=-1)

                if point_stack is None:
                    point_stack = new_array.copy()
                else:
                    point_stack = np.concatenate((point_stack, new_array))

    return point_stack


def crop_point_cloud(pcd, building, buffer_size=1.0):
    """
    三次元点群を建物ポリゴンで切り取る。

    Parameters
    ----------
    pcd: o3d.geometry.PointCloud
        三次元点群データ
    building: geopandas.GeoDataFrame
        切り取る建物ポリゴン (PolygonZ) のリスト、
        0 番目に底面のジオメトリを含む。
        CRS は pcd に合わせて変換済みであること。
    buffer_size: float
        バッファサイズ（単位は CRS による）

    Returns
    -------
    o3d.geometry.PointCloud
        切り出した三次元点群データ
    """
    geometry2d = building.iloc[0].geom
    boundary = geometry2d.buffer(buffer_size)

    # 高さ 100 の柱状ボリュームを作成
    select_vol = o3d.visualization.SelectionPolygonVolume()
    select_vol.orthogonal_axis = "z"
    select_vol.axis_min = 0
    select_vol.axis_max = 100
    bldg_ext_pts = list(boundary.exterior.coords)
    polygon = np.array(bldg_ext_pts, dtype=np.float64)
    polygon = np.insert(polygon, 2, 0, axis=1)
    logger.debug(polygon)
    select_vol.bounding_polygon = o3d.utility.Vector3dVector(polygon)

    # Crop
    pcd_cropped = select_vol.crop_point_cloud(pcd)

    return pcd_cropped


def crop_las(
        boundary: [float, float, float, float],
        lasfiles: List[os.PathLike]):
    """
    LAS ファイルから指定した領域に含まれる三次元点群を切り出す。

    Parameters
    ----------
    boundary: List[float, float, float, float]
        対象領域の x0, y0, x1, y1 (平面直角座標系）
    lasfiles: List[os.PathLike]
        読み込む LAS ファイルのリスト

    Returns
    -------
    o3d.geometry.PointCloud
    """
    # 三次元点群のOpen3Dへの読み込み
    # https://github.com/colspan/lasto3dtiles
    las_array = read_lasfiles(boundary, lasfiles)
    if las_array is None:
        logger.warning("LAS データが存在しませんでした。")
        return o3d.geometry.PointCloud()
        # raise RuntimeError("No LAS data in this server.")

    logger.info("LAS データを ndarray に読み込み完了 ({})".format(
        las_array.shape[0]))

    pcd = o3d.geometry.PointCloud()  # CRS is equal to LAS file
    pcd.points = o3d.utility.Vector3dVector(las_array[:, 0:3])
    # detect color schema
    if np.sum(las_array[0, 4:7]) > 0:
        colors = las_array[:, 4:7]
    else:
        colors = np.ndarray((las_array.shape[0], 3), dtype=np.float32)
        for i in range(3):
            colors[:, i] = las_array[:, 3]
    pcd.colors = o3d.utility.Vector3dVector(colors)

    logger.info("三次元点群をOpen3Dに読み込み完了")
    return pcd
