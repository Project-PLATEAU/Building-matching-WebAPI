from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, BigInteger, String, Numeric
from geoalchemy2 import Geometry, Geography, shape

Base = declarative_base()


class Plateau(Base):
    __tablename__ = 'plateau_buildings_lod1'
    fid = Column(BigInteger, primary_key=True)
    bldid = Column(String)
    geom = Column(Geometry('POLYGON'))
    area = Column(Numeric)

    def get_shapely_geometry(self):
        """
        Returns geom as Shapely geometry.
        """
        return shape.to_shape(self.geom)


class Plateau_LOD2(Base):
    __tablename__ = 'plateau_buildings_lod2'
    fid = Column(BigInteger, primary_key=True)
    bldid = Column(String)
    geom = Column(Geometry('POLYGON'))
    area = Column(Numeric)

    def get_shapely_geometry(self):
        """
        Returns geom as Shapely geometry.
        """
        return shape.to_shape(self.geom)
