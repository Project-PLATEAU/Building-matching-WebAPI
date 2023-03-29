import json
from logging import getLogger
import os
from typing import List, Optional

import geopandas as gpd
import shapely
from shapely.geometry import Polygon
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .model import Plateau, Plateau_LOD2

logger = getLogger(__name__)


class Database(object):
    # ref: https://geoalchemy-2.readthedocs.io/en/latest/orm_tutorial.html

    def __init__(self, echo: bool = False):
        self.dbuser = dbuser = os.environ.get('POSTGRES_USER', 'pguser')
        self.dbpass = dbpass = os.environ.get('POSTGRES_PASSWORD', 'pgpass')
        self.dbhost = dbhost = os.environ.get('POSTGRES_HOST', 'localhost')
        self.dbport = dbport = os.environ.get('POSTGRES_PORT', 5432)
        self.dbname = dbname = os.environ.get('POSTGRES_DB', 'pgdb')
        self.engine = create_engine(
            f'postgresql://{dbuser}:{dbpass}@{dbhost}:{dbport}/{dbname}',
            echo=echo)

        self.Session = sessionmaker(bind=self.engine)
        self.current_session = None

    def __del__(self):
        self.close_session()

    def close_session(self):
        if self.current_session:
            self.current_session.close()

        self.current_session = None

    def get_session(self):
        if self.current_session:
            return self.current_session
        else:
            self.current_session = self.Session()

        return self.current_session

    def create_table(self, tablename: str, features: list):
        """
        Feature Collection からテーブルを作成する。

        Parameters
        ----------
        tablename: str
            作成するテーブル名
        features: List[feature]
            レコードとして登録するfeature のリスト
        """
        gdf = gpd.GeoDataFrame.from_features(features, crs='EPSG:4326')

        with self.engine.connect() as con:
            # PostGISに出力 (オプション： データ置き換え、indexなし)
            gdf.to_postgis(
                tablename,
                con,
                if_exists='replace',
                index=False,
            )

    """
    Plateau 操作メソッド
    """

    def check_plateau_table_exists(self) -> bool:
        from sqlalchemy.exc import ProgrammingError
        try:
            self.get_session().query(Plateau.bldid).first()
        except ProgrammingError:
            self.close_session()
            return False
        return True

    def get_plateau_by_fid(self, fid: int) -> Optional[Plateau]:
        plateau = self.get_session().query(Plateau).get(fid)
        return plateau

    def get_plateau_by_bldid(self, bldid: str) -> Optional[Plateau]:
        plateau = self.get_session().query(Plateau).filter(
            Plateau.bldid == bldid).first()
        return plateau

    def get_plateau_building(self, bldid: str, lod: int = 1) -> Optional:
        """
        Plateau 建物IDを指定して PostgreSQL から検索し、
        geopandas オブジェクトを作成して返す。

        Parameters
        ----------
        bldid: str
            Plateau bldID
        lod: int
            LOD を指定する。省略した場合は 1。
        """
        tablename = Plateau.__tablename__
        if lod == 2:
            tablename = Plateau_LOD2.__tablename__

        with self.engine.connect() as con:
            sql = (
                "SELECT fid, bldid, geom3d AS geom FROM "
                f"{tablename} WHERE bldID=%s")
            gdf = gpd.GeoDataFrame.from_postgis(
                sql=sql,
                con=con,
                geom_col="geom",
                params=[bldid])

            if len(gdf) == 0:
                return None

            # Convert MultiPolygonZ to list of PolygonZ objects
            exploded = gdf.explode(index_parts=True)

            return exploded

    def get_plateau_building_2d(self, bldid: str, lod: int = 1) -> Optional:
        """
        Plateau 建物IDを指定して PostgreSQL から検索し、
        底面の geopandas オブジェクトを作成して返す。

        Parameters
        ----------
        bldid: str
            Plateau bldID
        lod: int
            LOD を指定する。省略した場合は 1。
        """
        tablename = Plateau.__tablename__
        if lod == 2:
            tablename = Plateau_LOD2.__tablename__

        with self.engine.connect() as con:
            sql = (
                "SELECT fid, bldid, geom FROM "
                f"{tablename} WHERE bldID=%s")
            gdf = gpd.GeoDataFrame.from_postgis(
                sql=sql,
                con=con,
                geom_col="geom",
                params=[bldid])

            if len(gdf) == 0:
                return None

            return gdf

    def join_table_with_plateau(self, tablename: str) -> List[Plateau]:
        """
        指定したテーブルと Plateau 2D テーブルを結合し、
        Plateau レコードを返す。

        Paramters
        ---------
        tablename: str
            結合するテーブル名

        Returns
        -------
        List[Plateau]
            マッチする Plateau レコードのリスト

        Note
        ----
        マッチングの条件は以下の通り。
        - Plateau ポリゴンと検索ポリゴンが交差する部分の面積が
          検索ポリゴン全体の面積の 0.2 倍以上
        """
        with self.Session() as sess:
            sql0 = f"""
                CREATE TEMPORARY TABLE polygons AS (
                SELECT
                    geoms.*,
                    ST_Area(geoms.__geom::geography) AS __area
                FROM (
                    SELECT
                        *, ST_Buffer((ST_Dump(geometry)).geom, 0) AS __geom
                    FROM {tablename}
                    ) geoms
                WHERE
                    GeometryType(geoms.__geom) = 'POLYGON'
                )
                """
            logger.debug("MultiPolygon を展開し Polygon を選択")
            sess.execute(sql0)
            logger.debug("空間インデックスを生成")
            sess.execute("ALTER TABLE polygons DROP COLUMN geometry")
            sess.execute((
                "CREATE INDEX idx_polygons_geom ON polygons"
                " USING gist(__geom)"))

            sql1 = f"""
                CREATE TEMPORARY TABLE joined AS (
                SELECT
                    polygons.*,
                    plateau.bldid AS plateau_bldid,
                    plateau.area AS plateau_area,
                    plateau.geom AS plateau_geom,
                    ST_Area(
                        ST_Intersection(
                            plateau.geom,
                            ST_Force2D(polygons.__geom)
                        )::geography
                    ) AS intersection_area,
                    polygons.__area AS source_area,
                    plateau.area / polygons.__area AS area_ratio,
                    ST_Distance(
                        ST_Centroid(plateau.geom)::geography,
                        ST_Centroid(polygons.__geom)::geography) AS dist
                FROM
                    "{Plateau.__tablename__}" AS plateau,
                    polygons
                WHERE
                    plateau.geom && polygons.__geom
                )
                """
            logger.debug("Polygon と Plateua を空間結合")
            sess.execute(sql1)

            sql2 = f"""
                SELECT
                    *,
                    ST_AsGeoJSON(plateau_geom) AS plateau_geom,
                    intersection_area / source_area > 0.4
                    OR intersection_area / plateau_area > 0.4 AS is_overlapped
                FROM
                    joined
                WHERE
                    intersection_area / source_area > 0.4
                    OR intersection_area / plateau_area > 0.4
                    OR (
                        dist < 10.0 AND area_ratio > 0.8 AND area_ratio < 1.2
                    )
                ORDER BY
                    plateau_bldid ASC,
                    is_overlapped DESC
                """
            logger.debug("面積比と重心間距離で絞り込み")
            results = sess.execute(sql2)

        return results

        sql = f"""
            WITH source AS (
                SELECT
                    geoms.*,
                    ST_Area(geoms.__geom::geography) AS __area
                FROM (
                    SELECT
                        *, (ST_Dump(geometry)).geom AS __geom
                    FROM {tablename}
                    ) geoms
                WHERE GeometryType(geoms.__geom) = 'POLYGON'
            )
            SELECT
                *,
                ST_AsGeoJSON(plateau_geom) AS plateau_geom,
                intersection_area / source_area > 0.4
                OR intersection_area / plateau_area > 0.4 AS is_overlapped
            FROM (
                SELECT
                    source.*,
                    plateau.bldid AS plateau_bldid,
                    plateau.area AS plateau_area,
                    plateau.geom AS plateau_geom,
                    ST_Area(
                        ST_Intersection(
                            plateau.geom,
                            ST_Force2D(source.__geom)
                        )::geography
                    ) AS intersection_area,
                    source.__area AS source_area,
                    plateau.area / source.__area AS area_ratio,
                    ST_Distance(
                        ST_Centroid(plateau.geom)::geography,
                        ST_Centroid(source.__geom)::geography) AS dist
                FROM
                    "{Plateau.__tablename__}" AS plateau,
                    source
                WHERE
                    plateau.geom && source.__geom
                ) s2
            WHERE
                intersection_area / source_area > 0.4
                OR intersection_area / plateau_area > 0.4
                OR (
                    dist < 10.0 AND area_ratio > 0.8 AND area_ratio < 1.2
                )
            ORDER BY
                plateau_bldid ASC,
                is_overlapped DESC
            """

        results = self.get_session().execute(sql)
        return results

    def search_by_polygon(self, polygon: Polygon) -> List[Plateau]:
        """
        指定した polygon とマッチする Plateau レコードを返す。

        Paramters
        ---------
        polygon: shapely.geometry.Polygon
            検索 Polygon, MultiPolygon も可

        Returns
        -------
        List[Plateau]
            マッチする Plateau レコードのリスト

        Note
        ----
        マッチングの条件は以下の通り。
        - Plateau ポリゴンと検索ポリゴンが交差する部分の面積が
          検索ポリゴン全体の面積の 0.2 倍以上
        """
        sql = f"""
            WITH polygons AS (
                SELECT
                    geoms.geom AS polygon,
                    ST_Area(geoms.geom::geography) AS area
                FROM
                    ( SELECT
                        (ST_Dump(ST_GeomFromEWKT(:polygon))).geom AS geom
                    ) geoms
                WHERE GeometryType(geoms.geom) = 'POLYGON'
            )
            SELECT
                fid AS plateau_fid,
                bldid AS plateau_bldid,
                area AS plateau_area,
                ST_AsGeoJSON(geom) AS plateau_geom,
                polygon_area,
                intersection_area,
                dist,
                intersection_area / polygon_area > 0.4
                OR intersection_area / area > 0.4 AS is_overlapped
            FROM (
                SELECT
                    plateau.*,
                    ST_Area(
                        ST_Intersection(
                            plateau.geom,
                            ST_Force2D(polygons.polygon)
                        )::geography
                    ) AS intersection_area,
                    polygons.area AS polygon_area,
                    plateau.area / polygons.area AS area_ratio,
                    ST_Distance(
                        ST_Centroid(plateau.geom)::geography,
                        ST_Centroid(polygons.polygon)::geography) AS dist
                FROM
                    "{Plateau.__tablename__}" AS plateau,
                    polygons
                WHERE
                    plateau.geom && polygons.polygon
                ) s2
            WHERE
                intersection_area / polygon_area > 0.4
                OR intersection_area / area > 0.4
                OR (
                    dist < 10.0 AND area_ratio > 0.8 AND area_ratio < 1.2
                )
            ORDER BY
                plateau_bldid ASC,
                is_overlapped DESC
            """

        query = self.get_session().execute(sql, {"polygon": polygon})

        results = []
        for row in query:
            results.append(dict(row))

        return results

        """
        (参考： ORM の場合の記述例，但し面積などは返せない)
        query = self.get_session().query(Plateau).filter(
            Plateau.geom.intersects(polygon),
            (func.ST_Area(
                cast(func.ST_Intersection(
                    func.ST_Force2D(Plateau.geom), polygon),
                    Geography(srid=4326))) / func.ST_Area(
                cast(polygon, Geography(srid=4326)))) > 0.2
        )
        """

    def search_plateau_intersects_polygon(
            self, polygon: Polygon) -> List[Plateau]:
        """
        指定した polygon に含まれる Plateau レコードを返す。

        Paramters
        ---------
        polygon: shapely.geometry.Polygon
            検索 Polygon

        Returns
        -------
        List[Plateau]
            マッチする Plateau レコードのリスト

        Note
        ----
        マッチングの条件は以下の通り。
        - Plateau ポリゴンと検索ポリゴンの BDR が交差する
        """
        sql = f"""
            SELECT
                plateau.fid AS plateau_fid,
                plateau.bldid AS plateau_bldid,
                plateau.area AS plateau_area,
                ST_AsGeoJSON(plateau.geom) AS plateau_geom
            FROM
                "{Plateau.__tablename__}" AS plateau
            WHERE
                plateau.geom && ST_Transform(ST_GeomFromEWKT(:polygon), 4326)
            ORDER BY
                plateau_bldid ASC
            """

        query = self.get_session().execute(sql, {"polygon": polygon})

        results = []
        for row in query:
            results.append(dict(row))

        return results

    def search_plateau_intersects_polygon_as_geojson(
            self, polygon: Polygon) -> dict:
        """
        指定した polygon に含まれる Plateau レコードの集合を
        GeoJSON (FeatureCollection) として返す。

        Paramters
        ---------
        polygon: shapely.geometry.Polygon
            検索 Polygon

        Returns
        -------
        dict
            FeatureCollection タイプの GeoJSON
        """
        results = self.search_plateau_intersects_polygon(polygon)

        # マッチング結果を FeatureCollection に変換
        features = []
        for r in results:
            # GeoJSON 表現の Polygon を読み込み
            # 頂点座標列を反時計回りに並べ替え
            polygon = shapely.geometry.shape(json.loads(r["plateau_geom"]))
            polygon = shapely.geometry.polygon.orient(polygon, sign=1.0)

            # Feature を生成
            feature = {
                "type": "Feature",
                "geometry": shapely.geometry.mapping(polygon),
                "properties": {
                    "plateau_fid": r["plateau_fid"],
                    "plateau_bldid": r["plateau_bldid"],
                    "plateau_area": round(r["plateau_area"], 4),
                },
            }
            features.append(feature)

        feature_collection = {
            "type": "FeatureCollection",
            "features": features,
        }

        return feature_collection


# Singleton
db = Database()
