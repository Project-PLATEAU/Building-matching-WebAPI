GDAL の GML 操作に関するドキュメント

ref:
- https://gdal.org/drivers/vector/gml.html
- https://gdal.org/drivers/vector/pg.html


GeometryType の一覧は [ogr_core.h](https://github.com/OSGeo/gdal/blob/8943200d5fac69f0f995fc11af7e7e3696823b37/gdal/ogr/ogr_core.h#L314-L402) 参照。

- コンテナ起動

        $ docker compose up -d pg15

- CityGML から PostGIS 変換

gdal コンテナ内で変換スクリプトを実行。

        $ docker compose run --rm gdal bash /conf/store_pl
ateau.sh

- PostGIS での変換

pg15 コンテナで変換する。

        $ docker compose exec pg15 psql -U postgres -f /conf/01_convert_plateau.sql

pg15 コンテナでバックアップを作成する。

        $ docker compose exec pg15 pg_dump -U postgres -t plateau_buildings_lod1 -t plateau_buildings_lod2 --no-owner | gzip -c > pgdb-YYYYMMDD.dump.gz

