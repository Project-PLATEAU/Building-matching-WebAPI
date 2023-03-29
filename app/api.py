import contextlib
import copy
import datetime
import functools
import gc
import glob
import json
from logging import getLogger
import os
import random
import resource
import tempfile
import zipfile

from flask import Blueprint, jsonify, request, Response, send_file
import laspy
import numpy as np
import open3d as o3d
import psutil
import shapely

from .database import db
from .zukaku import get_extent_polygon
from .build3d import Build3d

logger = getLogger(__name__)

api = Blueprint('api', __name__, url_prefix='/api')
random.seed()


@contextlib.contextmanager
def limit_resource(limit: int, type=resource.RLIMIT_DATA):
    """
    この内部で呼び出される処理で利用できるメモリを制限する。
    ref: https://stackoverflow.com/questions/13622706/how-to-protect-myself-from-a-gzip-or-bzip2-bomb

    Parameters
    ----------
    limit: int
        利用可能なメモリ(バイト)
    type: resource
        指定するリソースタイプ

    Notes
    -----
    このクロージャから抜けるとき、制限は元に戻る。
    """
    soft_limit, hard_limit = resource.getrlimit(type)
    resource.setrlimit(type, (limit, hard_limit))  # set soft limit
    logger.info("メモリ上限を {}B / {}B に制限します。".format(
        limit, hard_limit))
    try:
        yield
    finally:
        resource.setrlimit(type, (soft_limit, hard_limit))  # restore
        logger.info("メモリ上限を {}B / {}B に戻します。".format(
            soft_limit, hard_limit))


def check_memory_usage():
    """
    使用中メモリ量をログに出力する
    """
    process = psutil.Process(os.getpid())
    mem_used = process.memory_info()[0] / float(2 ** 20)
    logger.info("使用メモリ： {:.3f} MB".format(mem_used))


def gc_final(function):
    """
    関数実行終了時に必ず gc を実行するデコレータ
    """
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            result = function(*args, **kwargs)
            return result
        finally:
            logger.info("関数 '{}' 実行後の GC:".format(function))
            process = psutil.Process(os.getpid())
            mem_used0 = process.memory_info()[0] / float(2 ** 20)
            gc.collect()
            mem_used1 = process.memory_info()[0] / float(2 ** 20)
            logger.info("使用メモリ： {:.3f} MB(GC前) -> {:.3f} MB(GC後)".format(
                mem_used0, mem_used1))

    return wrapper


@api.route('/building2d', methods=['POST'])
def building2d() -> str:
    """
    Post された GeoJSON ポリゴンにマッチする
    Plateau 建物の情報を返す。

    Parameters
    ----------
    geojson: str
        検索したい建物の GeoJSON
        Feature または Polygon を受け付ける

    Returns
    -------
    str (GeoJSON, FeatureCollection)
        検索結果を JSON エンコーディングした文字列
    """
    if db.check_plateau_table_exists() is False:
        raise RuntimeError("データベースをリストアしてください。")

    logger.debug("アップロード開始")
    obj = request.json

    features = []

    geojson_type = obj.get("type", "(no type)")
    if geojson_type.lower() == "feature":
        features = [obj]
    elif geojson_type.lower() == "polygon":
        features = [{
            "type": "feature",
            "properties": {},
            "geometry": obj
        }]
    elif geojson_type.lower() == "featurecollection":
        features = obj["features"]
    else:
        return jsonify(f"Invalid geojson type, {geojson_type}"), 400

    return Response(match_features_generator(features))


def match_features_generator(features: list, size: int = 4096):
    """
    GeoJSON Features を指定したサイズで分割し、
    順番にマッチングした結果をまとめて FeatureCollection として
    Response にストリーミング出力するジェネレータ。

    Parameters
    ----------
    features: list
        GeoJSON 形式の Feature リスト
    size: int
        分割するサイズ
    """
    yield '{"type":"FeatureCollection","features":['

    assigned = set()

    for n in range(0, len(features), size):
        # テーブルを生成
        # ToDo: 本来は temporary table が望ましい
        tablename = "tmp_features_{:05d}".format(random.randrange(100000))
        logger.debug("GeoJSON ({}:{}) をテーブル {} に登録開始".format(
            n, n + size, tablename))
        gdf = db.create_table(
            tablename=tablename,
            features=features[n: n + size]
        )

        logger.debug("Plateau テーブルと空間結合開始")
        results = db.join_table_with_plateau(tablename=tablename)

        for i, row in enumerate(results):
            # GeoJSON 表現の Polygon を読み込み
            # 頂点座標列を反時計回りに並べ替え
            r = dict(row)
            polygon = shapely.geometry.shape(json.loads(r["plateau_geom"]))
            polygon = shapely.geometry.polygon.orient(polygon, sign=1.0)

            confidence = "low"
            if r["is_overlapped"]:
                confidence = "high"
                assigned.add(r["plateau_bldid"])
            elif r["plateau_bldid"] in assigned:
                continue

            # Properties を生成
            properties = {}
            for k, v in r.items():
                if k in ('__area', '__geom',
                         'plateau_geom', 'geometry', 'is_overlapped'):
                    continue
                elif k in ('plateau_area', 'source_area', 'intersection_area'):
                    v = round(v, 4)
                elif k in ('dist', 'area_ratio'):
                    v = round(v, 2)

                properties[k] = v

            properties['confidence'] = confidence  # 末尾に追加

            # Feature を生成
            feature = {
                "type": "Feature",
                "geometry": shapely.geometry.mapping(polygon),
                "properties": properties
            }
            if n + i == 0:
                yield "\n"
            else:
                yield ",\n"

            yield json.dumps(feature, ensure_ascii=False)

        # テーブルを削除
        with db.get_session() as session:
            session.execute(f"DROP TABLE {tablename}")
            logger.debug("テーブル {} を削除".format(tablename))
            session.commit()

    yield "]}"
    logger.debug("マッチング完了（{} features）".format(n + i))


@api.route('/search-plateau', methods=['GET'])
def search_plateau() -> str:
    """
    指定された bldid の Plateau 建物情報を返す。

    Parameters
    ----------
    bldid: str
        Plateau 建物の bldid

    Returns
    -------
    str (GeoJSON, Feature)
        検索結果を JSON エンコーディングした文字列
    """
    if db.check_plateau_table_exists() is False:
        raise RuntimeError("データベースをリストアしてください。")

    bldid = request.args.get("bldid")
    r = db.get_plateau_by_bldid(bldid)
    if r is None:
        return "No plateau building is found with bldid='{}'".format(
            bldid), 403

    # Feature を生成
    feature = {
        "type": "Feature",
        "geometry": shapely.geometry.mapping(r.get_shapely_geometry()),
        "properties": {
            "plateau_fid": r.fid,
            "plateau_bldid": r.bldid,
            "plateau_area": round(r.area, 4),
        },
    }

    return jsonify(feature), 200


@api.route('/search-plateau-in', methods=['POST'])
def search_plateau_in_geojson() -> str:
    """
    指定された Polygon と交差する Plateau 建物情報のリストを返す。

    Parameters
    ----------
    request_body: str
        GeoJSON (Polygon のみ)
        または {"meshcode": <meshcode>}

    Returns
    -------
    str (GeoJSON, Feature)
        検索結果を JSON エンコーディングした文字列
    """
    if db.check_plateau_table_exists() is False:
        raise RuntimeError("データベースをリストアしてください。")

    obj = request.json
    geojson_type = obj.get("type", "(no type)")
    if geojson_type.lower() == "polygon":
        polygon = shapely.geometry.shape(obj)
    elif obj.get("meshcode"):
        polygon = get_extent_polygon(obj.get("meshcode"))
    else:
        return jsonify(f"Invalid geojson type, {geojson_type}"), 400

    feature_collection = db.search_plateau_intersects_polygon_as_geojson(
        "SRID=4326;" + shapely.wkt.dumps(polygon))

    return jsonify(feature_collection), 200


@api.route('/search-plateau-in', methods=['GET'])
def search_plateau_in_meshcode() -> str:
    """
    指定された図郭番号と交差する Plateau 建物情報のリストを返す。

    Parameters
    ----------
    meshcode: str
        図郭番号（例： "08NE3801"）

    Returns
    -------
    str (GeoJSON, Feature)
        検索結果を JSON エンコーディングした文字列
    """
    if db.check_plateau_table_exists() is False:
        raise RuntimeError("データベースをリストアしてください。")

    meshcode = request.args.get("meshcode")
    try:
        polygon = get_extent_polygon(meshcode)
    except RuntimeError:
        return jsonify(f"Invalid meshcode: {meshcode}"), 400

    feature_collection = db.search_plateau_intersects_polygon_as_geojson(
        "SRID=4326;" + shapely.wkt.dumps(polygon))

    return jsonify(feature_collection), 200


@api.route('/zukaku', methods=['GET'])
def search_mesh() -> str:
    """
    指定された国土基本図図郭番号に対応するポリゴンを返す。

    Parameters
    ----------
    meshcode: str
        図郭番号

    Returns
    -------
    str (GeoJSON, Polygon)
        図郭番号に対応する Polygon の GeoJSON
    """
    meshcode = request.args.get("meshcode")
    polygon = get_extent_polygon(meshcode)

    geometry = shapely.geometry.mapping(polygon)
    return jsonify(geometry), 200


@api.route('/crop-las/<string:bldid>', methods=['GET'])
def crop_las(bldid: str):
    """
    Plateau 建物で LAS を切り出し、 PLY ファイルを返す。

    Parameters
    ----------
    bldid: str
        Plateau 建物の bldid
    limit: int
        点の数の最大値、この値以下になるまでダウンサンプリング
    """
    logger.info("処理開始")
    start_at = datetime.datetime.now()  # 処理開始時刻
    limit = request.args.get("limit", "10k")

    try:
        if limit is not None:
            if limit.lower().endswith('k'):
                limit = int(limit[:-1]) * 1000
            elif limit.lower().endswith('m'):
                limit = int(limit[:-1]) * 1000000
            else:
                limit = int(limit)

    except ValueError as e:
        return str(e), 400

    try:
        build3d = Build3d(
            bldid=bldid, system_code=8, dirname=None)
    except RuntimeError as e:
        return str(e), 400

    pcd = build3d.get_pointcloud(limit_points=limit)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.ply') as f:
        o3d.io.write_point_cloud(f.name, pcd)
        logger.debug("一時ファイルに出力完了")

        logger.info("処理時間：{:.3f} sec.".format(
            (datetime.datetime.now() - start_at).total_seconds()))

        return send_file(
            f.name,
            as_attachment=True,
            download_name="{}_{}.ply".format(bldid, len(pcd.points)),
            mimetype='text/plain')


@api.route('/obj3d/<string:bldid>', methods=['GET'])
@gc_final
def get_obj3d(bldid: str):
    """
    Plateau 建物で LAS を切り出し、テクスチャ付き3Dモデルを返す。

    Parameters
    ----------
    bldid: str
        Plateau 建物の bldid
    """
    if db.check_plateau_table_exists() is False:
        raise RuntimeError("データベースをリストアしてください。")

    logger.info("処理開始")
    start_at = datetime.datetime.now()  # 処理開始時刻

    imagesize = int(request.args.get("size", 512))
    method = request.args.get("method", "smart")
    lod = int(request.args.get("lod", 1))
    limit = request.args.get("limit", "10k")
    if lod not in (1, 2):
        lod = 1

    try:
        if limit is not None:
            if limit.lower().endswith('k'):
                limit = int(limit[:-1]) * 1000
            elif limit.lower().endswith('m'):
                limit = int(limit[:-1]) * 1000000
            else:
                limit = int(limit)

    except ValueError as e:
        return str(e), 400

    try:
        build3d = Build3d(
            bldid=bldid, system_code=8, lod=lod, dirname=None)
    except RuntimeError as e:
        return str(e), 400

    with tempfile.TemporaryDirectory() as tmpdirname:
        # ファイルを一時ディレクトリ内に出力する
        build3d.set_dirname(tmpdirname)

        # 点群の読み込みは make_objfiles() 中で自動敵に
        # 実行されるが、メッセージの順番が入れ替わるので
        # 先に読み込んでおく
        try:
            build3d.get_pointcloud(limit_points=limit)
        except RuntimeError as e:
            return str(e), 400

        npoints = len(build3d.get_pointcloud().points)

        # OBJ, MTL, テクスチャファイルを作成する
        max_memory = os.environ.get('MAX_MEMORY', 2 << 30)
        max_memory = int(max_memory)
        with limit_resource(limit=max_memory):
            try:
                build3d.make_objfiles(
                    imagesize=imagesize,
                    texture_mapping_method=method)
            except MemoryError as e:
                return str(e), 400
            finally:
                del build3d

        # ZIP ファイルを作成
        logger.debug("Zipfile を作成")
        zipfilename = os.path.join(tmpdirname, '{}.zip'.format(bldid))
        with zipfile.ZipFile(zipfilename, 'w') as zipf:
            for filename in glob.glob(
                    os.path.join(tmpdirname, '*')):
                if os.path.isdir(filename):
                    continue

                if filename.endswith('.zip'):
                    continue

                zipf.write(
                    filename,
                    arcname=os.path.basename(filename))

        logger.info("処理時間：{:.3f} sec.".format(
            (datetime.datetime.now() - start_at).total_seconds()))

        download_name = "obj3d_{}_lod{}_{}_{}_{}.zip".format(
            bldid, lod, method, imagesize, npoints)

        return send_file(
            zipfilename,
            as_attachment=True,
            download_name=download_name,
            mimetype='application/zip')


@api.route('/pointcloud3d', methods=['POST'])
@gc_final
def check_las() -> str:
    """
    アップロードされた LAS ファイルに含まれる
    Plateau 建物 ID のリストとテクスチャ網羅率を計算する。

    Parameters
    ----------
    request_body: str
        LAS ファイル
    srid: int
        LAS の座標系, デフォルトは 6676 (8系)

    Returns
    -------
    str (GeoJSON, FeatureCollection)
        検索結果を JSON エンコーディングした文字列
    """
    if db.check_plateau_table_exists() is False:
        raise RuntimeError("データベースをリストアしてください。")

    logger.info("処理開始")
    start_at = datetime.datetime.now()  # 処理開始時刻

    # 系番号
    srid = int(request.form.get('srid', '6676'))
    logger.info("SRID={}".format(srid))

    # POST データに file パートが含まれていることを確認
    if 'file' not in request.files:
        return 'No file part', 400

    file = request.files['file']
    # 空ファイルではないことを確認する
    if file.filename == '':
        return 'No selected file', 400

    if not file or not file.filename.lower().endswith('.las'):
        return "No LAS file.", 400

    boundary = None
    with tempfile.NamedTemporaryFile('w+b') as tmpf:
        logger.info("アップロードファイルを一時ファイルとして保存中")
        file.save(tmpf.name)
        logger.info("LAS データを読み込み中")
        with laspy.open(tmpf.name) as fh:
            hmins = fh.header.mins
            hmaxs = fh.header.maxs
            boundary = (hmins[0], hmins[1], hmaxs[0], hmaxs[1])

            # 1m 間隔の point cloud を作成
            las = fh.read()
            las_nparray = np.stack((las.x, las.y, las.z), axis=-1)
            pcd = o3d.geometry.PointCloud()
            pcd.points = o3d.utility.Vector3dVector(las_nparray)
            npoints = len(pcd.points)
            pcd = pcd.voxel_down_sample(voxel_size=1.0)
            del las

        logger.info("LAS データを読み込み完了 ({} points)".format(npoints))

    logger.info("データの範囲: {}".format(json.dumps(boundary)))

    polygon = shapely.geometry.Polygon([
        (boundary[0], boundary[1]),
        (boundary[0], boundary[3]),
        (boundary[2], boundary[3]),
        (boundary[2], boundary[1]),
        (boundary[0], boundary[1])])

    wkt = "SRID={};".format(srid) + shapely.wkt.dumps(polygon)
    logger.info("検索範囲: {}".format(wkt))
    results = db.search_plateau_intersects_polygon(wkt)
    if len(results) == 0:
        return "No Plateau buildings.", 400

    logger.info("範囲内に {} 件の Plateau 建物があります。".format(
        len(results)))

    # マッチング結果を FeatureCollection に変換
    features = []
    elasps = {"crop": 0.0, "matching": 0.0, "jsonify": 0.0}
    for r in results:
        logger.info("bldid:{} の一致率を計算中".format(
            r["plateau_bldid"]))
        at0 = datetime.datetime.now()
        build3d = Build3d(
            bldid=r["plateau_bldid"],
            system_code=srid - 6668,
            lod=2)
        build3d.pcd = build3d.crop_point_cloud(copy.copy(pcd))
        logger.debug("bldid:{} の点群を切り出し完了".format(
            r["plateau_bldid"]))
        at1 = datetime.datetime.now()
        elasps["crop"] += (at1 - at0).total_seconds()
        npoints_in_region = len(build3d.pcd.points)
        npoints_near_wall = build3d.count_points_near_walls(threshold=1.0)
        area = round(build3d.get_surface_area(), 2)
        logger.debug("bldid:{} の壁面とのマッチング完了".format(
            r["plateau_bldid"]))
        at2 = datetime.datetime.now()
        elasps["matching"] += (at2 - at1).total_seconds()

        # GeoJSON 表現の Polygon を読み込み
        # 頂点座標列を反時計回りに並べ替え
        polygon = shapely.geometry.shape(json.loads(r["plateau_geom"]))
        polygon = shapely.geometry.polygon.orient(polygon, sign=1.0)

        # Feature を生成
        feature = {
            "type": "Feature",
            "geometry": shapely.geometry.mapping(polygon),
            "properties": {
                "plateau_bldid": r["plateau_bldid"],
                "num_points_in_region": npoints_in_region,
                "num_points_near_wall": npoints_near_wall,
                "area": area,
            },
        }
        features.append(feature)
        logger.debug("bldid:{} の GeoJSON 生成完了".format(
            r["plateau_bldid"]))
        at3 = datetime.datetime.now()
        elasps["jsonify"] += (at3 - at2).total_seconds()

    feature_collection = {
        "type": "FeatureCollection",
        "features": features,
    }
    logger.info("総切り出し時間：{:.3f}".format(elasps["crop"]))
    logger.info("総マッチング時間：{:.3f}".format(elasps["matching"]))
    logger.info("総 JSON 化時間：{:.3f}".format(elasps["jsonify"]))
    logger.info("処理時間：{:.3f} sec.".format(
        (datetime.datetime.now() - start_at).total_seconds()))

    return jsonify(feature_collection), 200


@api.route('/mapping3d', methods=['POST'])
@gc_final
def match_las() -> str:
    """
    アップロードされた LAS ファイルを利用して
    指定された Plateau 建物にテクスチャを貼り付け、
    3D モデルを返す。

    Parameters
    ----------
    request_body: str
        LAS ファイル
    bldid: str
        対象の建物 ID
    srid: int
        LAS の座標系, デフォルトは 6676 (8系)
    imagesize: int
        壁面テクスチャ画像の長辺ピクセル数
    method: str
        テクスチャマッピング手法 (nearest/all/smart)
    lod: int
        対象建物の LOD
    limit: str
        点群の最大点数

    Returns
    -------
    str (GeoJSON, Feature)
        検索結果を JSON エンコーディングした文字列
    """
    if db.check_plateau_table_exists() is False:
        raise RuntimeError("データベースをリストアしてください。")

    logger.info("処理開始")
    start_at = datetime.datetime.now()  # 処理開始時刻

    # 系番号
    srid = int(request.form.get('srid', '6676'))
    logger.info("SRID={}".format(srid))

    # POST データに file パートが含まれていることを確認
    if 'file' not in request.files:
        return 'No file part', 400

    file = request.files['file']
    # 空ファイルではないことを確認する
    if file.filename == '':
        return 'No selected file', 400

    if not file or not file.filename.lower().endswith('.las'):
        return "No LAS file.", 400

    imagesize = int(request.form.get("size", 512))
    method = request.form.get("method", "smart")
    lod = int(request.form.get("lod", 1))
    limit = str(request.form.get("limit", "10k"))
    bldid = str(request.form.get("bldid"))

    if lod not in (1, 2):
        lod = 1

    try:
        if limit is not None:
            if limit.lower().endswith('k'):
                limit = int(limit[:-1]) * 1000
            elif limit.lower().endswith('m'):
                limit = int(limit[:-1]) * 1000000
            else:
                limit = int(limit)

    except ValueError as e:
        return str(e), 400

    if bldid is None:
        return "bldid is required.", 400

    try:
        build3d = Build3d(
            bldid=bldid, system_code=8, lod=lod, dirname=None)
    except RuntimeError as e:
        return str(e), 400

    with tempfile.TemporaryDirectory() as tmpdirname:
        # ファイルを一時ディレクトリ内に出力する
        build3d.set_dirname(tmpdirname)

        # 点群をアップロードされたファイルから読み込む
        try:
            with tempfile.NamedTemporaryFile('w+b') as tmpf:
                logger.info("アップロードファイルを一時ファイルとして保存中")
                file.save(tmpf.name)

                build3d.get_pointcloud(
                    limit_points=limit,
                    lasfiles=[tmpf.name])
        except RuntimeError as e:
            return str(e), 400

        npoints = len(build3d.get_pointcloud().points)

        # OBJ, MTL, テクスチャファイルを作成する
        max_memory = os.environ.get('MAX_MEMORY', 2 << 30)
        max_memory = int(max_memory)
        with limit_resource(limit=max_memory):
            try:
                build3d.make_objfiles(
                    imagesize=imagesize,
                    texture_mapping_method=method)
            except MemoryError as e:
                return str(e), 400
            finally:
                del build3d

        # ZIP ファイルを作成
        logger.debug("Zipfile を作成")
        zipfilename = os.path.join(tmpdirname, '{}.zip'.format(bldid))
        with zipfile.ZipFile(zipfilename, 'w') as zipf:
            for filename in glob.glob(
                    os.path.join(tmpdirname, '*')):
                if os.path.isdir(filename):
                    continue

                if filename.endswith('.zip'):
                    continue

                zipf.write(
                    filename,
                    arcname=os.path.basename(filename))

        logger.info("処理時間：{:.3f} sec.".format(
            (datetime.datetime.now() - start_at).total_seconds()))

        download_name = "obj3d_{}_lod{}_{}_{}_{}.zip".format(
            bldid, lod, method, imagesize, npoints)

        return send_file(
            zipfilename,
            as_attachment=True,
            download_name=download_name,
            mimetype='application/zip')
