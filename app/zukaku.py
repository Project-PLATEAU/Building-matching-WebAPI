"""
国土交通省 「標準図式」に基づく「図郭」と経緯度の相互変換
https://www.mlit.go.jp/common/001248461.pdf
"""

from logging import getLogger
from typing import List, Optional, Tuple
import re

logger = getLogger(__name__)


def get_extent(code: str) -> Tuple[int, int, int, int, str, int]:
    """
    指定された区画の北西端のx,y、南東端のx,y、CRS
    および地図情報レベルを返す。

    Parameter
    ---------
    code: str
        区画 (ex. '08NE3801')

    Returns
    -------
    tuple(min_x, min_y, max_x, max_y, crs, int)
        区画の北西端と南東端の座標, CRS, 地図情報レベル
    """
    m = re.match(r'([0-9]{2})?([A-Z]{2})([0-9A-T]*)', code)
    system_code, kukaku, numbers = m.groups()
    logger.debug("系:{}, 区画:{}, 数値:{}".format(system_code, kukaku, numbers))

    # 第84条4の一
    x0 = (-160 + (ord(kukaku[1]) - 65) * 40) * 1000
    y0 = (300 - (ord(kukaku[0]) - 65) * 30) * 1000
    dx, dy = 40000, 30000
    level = 50000

    if len(numbers) >= 2:
        # 同二 地図情報レベル5000
        x0 += (ord(numbers[1]) - 48) * 4000
        y0 -= (ord(numbers[0]) - 48) * 3000
        dx, dy = 4000, 3000
        level = 5000

    if len(numbers) == 3:
        # 同三 地図情報レベル2500
        if numbers[2] in ('2', '4'):
            x0 += 2000

        if numbers[2] in ('3', '4'):
            y0 -= 1500

        dx, dy = 2000, 1500
        level = 2500

    if len(numbers) >= 4:
        if ord(numbers[2]) >= 48 and ord(numbers[2]) < 58:  # 0-9
            if ord(numbers[3]) >= 65 and ord(numbers[3]) < 70:  # A-E
                # 同四 地図情報レベル1000
                x0 += (ord(numbers[3]) - 65) * 800
                y0 -= (ord(numbers[2]) - 48) * 600
                dx, dy = 800, 600
                level = 1000

            elif ord(numbers[3]) >= 48 and ord(numbers[3]) < 58:
                # 同五 地図情報レベル500
                x0 += (ord(numbers[3]) - 48) * 400
                y0 -= (ord(numbers[2]) - 48) * 300
                dx, dy = 400, 300
                level = 500

        elif ord(numbers[2]) >= 65 and ord(numbers[2]) < 85:  # A-T
            # 同六 地図情報レベル250
            x0 += (ord(numbers[3]) - 65) * 200
            y0 -= (ord(numbers[2]) - 65) * 150
            dx, dy = 200, 150
            level = 250

    if len(numbers) == 5:
        # 独自拡張 2分割
        dx, dy = dx / 2, dy / 2
        if ord(numbers[4]) in ('2', '4'):
            x0 += dx

        if ord(numbers[4]) in ('3', '4'):
            y0 -= dy

        level /= 2

    if len(numbers) == 6:
        if ord(numbers[4]) >= 48 and ord(numbers[4]) < 58:  # 0-9
            if ord(numbers[5]) >= 65 and ord(numbers[5]) < 70:  # A-E
                # 独自拡張 5分割
                dx, dy = dx / 5, dy / 5
                x0 += (ord(numbers[5]) - 65) * dx
                y0 -= (ord(numbers[4]) - 48) * dy
                level /= 5

            elif ord(numbers[5]) >= 48 and ord(numbers[5]) < 58:  # 0-9
                # 独自拡張 10分割
                dx, dy = dx / 10, dy / 10
                x0 += (ord(numbers[5]) - 48) * dx
                y0 -= (ord(numbers[4]) - 48) * dy
                level /= 10

        elif ord(numbers[4]) >= 65 and ord(numbers[4]) < 85:  # A-T
            # 独自拡張 20分割
            dx, dy = dx / 20, dy / 20
            x0 += (ord(numbers[5]) - 65) * dx
            y0 -= (ord(numbers[4]) - 65) * dy
            level /= 20

    if system_code:
        crs = 'EPSG:{:4d}'.format(6668 + int(system_code))
    else:
        crs = None

    return (x0, y0 - dy, x0 + dx, y0, crs, level)


def get_extent_polygon(meshcode: str):
    """
    指定された区画を表す Shaply Polygon を返す。

    Parameter
    ---------
    code: str
        区画 (ex. '08NE3801')

    Returns
    -------
    shapely.geometry.Polygon
        WGS84 に変換したポリゴン
    """
    import pyproj
    import shapely.geometry

    x0, y0, x1, y1, crs, level = get_extent(meshcode)

    from_crs = pyproj.CRS(crs)
    wgs84 = pyproj.CRS('EPSG:4326')
    transformer = pyproj.Transformer.from_crs(from_crs, wgs84, always_xy=True)

    lon0, lat0 = transformer.transform(x0, y0)
    lon1, lat1 = transformer.transform(x1, y1)

    polygon = shapely.geometry.Polygon([
        [lon0, lat0], [lon0, lat1],
        [lon1, lat1], [lon1, lat0], [lon0, lat0]])
    polygon = shapely.geometry.polygon.orient(polygon, sign=1.0)
    return polygon


def get_code(
        x: float, y: float,
        system_code: Optional[int] = None,
        level: int = 5000) -> str:
    """
    指定された座標を含む図郭番号を返す。

    Parameter
    ---------
    x, y: float
        X および Y 座標
    system_code: int
        系番号, 省略した場合は先頭二文字が省略される
    level: int
        地図情報レベル，省略した場合は 5000

    Returns
    -------
    string
        図郭番号
    """
    if abs(x) >= 160000 or abs(y) >= 300000:
        raise ValueError("X and/or Y values are out of range.")

    code = ''
    if system_code:
        # 系
        code = '{:02d}'.format(system_code)

    # 「0を含むマイナス値」の境界判定を避けるため、yの値を反転しておく
    x = int(x)
    y = int(-y)

    # 第84条4の一：区画名
    code += chr(75 + y // 30000) + chr(69 + x // 40000)  # [A-T][A-H]
    if level > 5000:
        return code

    # 同二 地図情報レベル5000
    x %= 40000
    y %= 30000
    code += chr(48 + y // 3000) + chr(48 + x // 4000)  # [0-9][0-9]
    if level > 2500:
        return code

    x %= 4000
    y %= 3000

    if level == 2500:
        # 同三 地図情報レベル2500
        if x < 2000.0:
            if y < 1500.0:
                code += '1'
            else:
                code += '3'
        else:
            if y < 1500.0:
                code += '2'
            else:
                code += '4'

        return code

    if level == 1000:
        # 同四 地図情報レベル1000
        code += chr(548 + y // 600) + chr(65 + x // 800)  # [0-4][A-E]
        return code

    if level == 250:
        # 同六 地図情報レベル250
        code += chr(65 + y // 150) + chr(65 + x // 200)  # [A-T][A-T]
        return code

    # 同五 地図情報レベル500
    code += chr(48 + y // 300) + chr(48 + x // 400)  # [0-9][0-9]
    if level == 500:
        return code

    x %= 400
    y %= 300

    if level == 50:
        # 独自拡張 地図情報レベル500 をさらに各辺10等分
        code += chr(48 + y // 30) + chr(48 + x // 40)  # [0-9][0-9]
        return code

    raise ValueError(
        "Unkown level, supported levels are: 5000,2500,1000,500,250,50")


def get_codes_in_area(
        x0: float, y0: float, x1: float, y1: float,
        system_code: Optional[int] = None,
        level: int = 5000) -> List[str]:
    """
    x0,y0 と x1,y1 を対角線とする長方形領域に含まれる図郭のコードのリストを返す。

    Parameter
    ---------
    x0, y0, x1, y1: float
        X および Y 座標
    system_code: int
        系番号, 省略した場合は先頭二文字が省略される
    level: int
        地図情報レベル，省略した場合は 5000

    Returns
    -------
    List[string]
        図郭番号のリスト
    """
    if level not in (50000, 5000, 2500, 1000, 500, 250, 50):
        raise ValueError(
            "Unkown level, supported levels are: 5000,2500,1000,500,250,50")

    dx, dy = (40000 * level / 50000, 30000 * level / 50000)
    if x0 > x1:
        x0, x1 = x1, x0

    if y0 > y1:
        y0, y1 = y1, y0

    codes = []
    x, y = x0, y0
    while True:
        while True:
            codes.append(get_code(x, y, system_code=system_code, level=level))
            if y > y1:
                break

            y += dy

        if x > x1:
            break

        x += dx
        y = y0

    return codes


if __name__ == '__main__':
    print(get_extent_polygon('08NE3801'))
    print(get_extent('08NE3801'))
    print(get_extent('NE'))
    print(get_code(32400, -129000, 8, 500))
    print(get_codes_in_area(32400, -99000, 32600, -99300, 8, 50))
    print(get_codes_in_area(
        32676.00220000071, -99170.90840027311,
        32704.777800000735, -99142.45190027489,
        8, 50))
