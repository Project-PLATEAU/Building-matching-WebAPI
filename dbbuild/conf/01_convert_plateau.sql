--
-- GDAL を利用して PostGIS にインポートした
-- テーブル plateau に含まれるジオメトリから
-- WebAPI 用のテーブルに変換するスクリプト
--
-- Usage:
-- psql postgres postgres -f /sql/01_convert_plateau.sql

--
-- LoD1
--

-- LoD1 のテーブルを作成
-- PolyhedralSurfaceZ を MultiPolygonZ に分解する
DROP TABLE IF EXISTS polygonz;
CREATE TABLE polygonz AS
SELECT
  c.bldid,
  ST_Collect(c.geom) AS geom3d
FROM (
  SELECT
    bldid,
    (ST_Dump(lod1)).geom
  FROM plateau
) c
GROUP BY c.bldid;

-- WebAPI 用のテーブルを作成する
DROP TABLE IF EXISTS public.plateau_buildings;
CREATE TABLE public.plateau_buildings (
  fid bigserial PRIMARY KEY,
  bldid text,
  geom geometry(Polygon,4326),
  geom3d geometry(MultiPolygonZ,4326),
  area double precision
);

-- WebAPI 用のテーブルに 2D と 3D のジオメトリを格納する
INSERT INTO
  public.plateau_buildings (bldid, geom, geom3d)
SELECT
  bldid,
  ST_SetSRID(
    (ST_Dump(ST_Buffer(ST_Force2D(geom3d), 0))).geom,
    4326),
  ST_SetSRID(geom3d, 4326)
FROM
  polygonz;

-- 2D ポリゴンの面積を事前に計算しておく
UPDATE
  public.plateau_buildings
SET
  area=ST_Area(geom::geography);

-- LoD1 のテーブル名に変更してインデックスを作成
DROP TABLE IF EXISTS public.plateau_buildings_lod1;
ALTER TABLE plateau_buildings
RENAME TO plateau_buildings_lod1;

CREATE INDEX idx_plateau_buildings_lod1_geom
ON plateau_buildings_lod1
USING gist(geom);

--
-- LoD2
--

-- LoD2 のテーブルを作成
-- PolyhedralSurfaceZ を MultiPolygonZ に分解する
DROP TABLE IF EXISTS polygonz;
CREATE TABLE polygonz AS
SELECT
  c.bldid,
  ST_Collect(c.geom) AS geom3d
FROM (
  SELECT
    bldid,
    (ST_Dump(lod2)).geom
  FROM plateau
) c
GROUP BY c.bldid;

-- WebAPI 用のテーブルを作成する
DROP TABLE IF EXISTS public.plateau_buildings;
CREATE TABLE public.plateau_buildings (
  fid bigserial PRIMARY KEY,
  bldid text,
  geom geometry(Polygon,4326),
  geom3d geometry(MultiPolygonZ,4326),
  area double precision
);

-- WebAPI 用のテーブルに 2D と 3D のジオメトリを格納する
INSERT INTO
  public.plateau_buildings (bldid, geom, geom3d)
SELECT
  bldid,
  ST_SetSRID(
    (ST_Dump(ST_Buffer(ST_Force2D(geom3d), 0))).geom,
    4326),
  ST_SetSRID(geom3d, 4326)
FROM
  polygonz;

-- 2D ポリゴンの面積を事前に計算しておく
UPDATE
  public.plateau_buildings
SET
  area=ST_Area(geom::geography);

-- LoD2 のテーブル名に変更してインデックスを作成
DROP TABLE IF EXISTS public.plateau_buildings_lod2 ;
ALTER TABLE plateau_buildings
RENAME TO plateau_buildings_lod2;

CREATE INDEX idx_plateau_buildings_lod2_geom
ON plateau_buildings_lod2
USING gist(geom);
