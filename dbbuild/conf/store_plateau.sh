#!/bin/bash

export GML_GFS_TEMPLATE=/conf/plateau_buildings.gfs

for i in /data/*.gml
do
	# `-forceNullable` is required to avoid
	# the "gml_id will be null" problem when loading FY2021 data.
	ogr2ogr -f postgresql postgresql://postgres:postgres@pg15/postgres $i -nln plateau -nlt GEOMETRYZ -skipfailures -append -lco SPATIAL_INDEX=NONE -forceNullable
done

echo "Finish."
