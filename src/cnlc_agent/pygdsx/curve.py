import h5py, warnings, json, time
import numpy as np
from typing import Union, Any
import struct

from cnlc_agent.pygdsx.unified_utils import (
    unite2str,
    hdf5_dataset2json_object,
    json_object2numpy,
    get_the_only_log_group,
    check_gdsx_file_exist_and_return_flow,
)


class BDCurveInfo:
    def __init__(self):
        self.majorver = 0
        self.minorver = 0
        self.typeNo = 0x10000001
        self.dimension = 2
        self.modifytime = 0
        self.type = 0
        self.recordType = 0

        self.dateDimensionType = 13
        self.dateDimensionStart = 0
        self.dateDimensionEnd = 0
        self.dateDimensionStep = 0
        self.dateDimensionLength = 1

        self.dimension1Type = 5
        self.dimension1Start = 0
        self.dimension1End = 0
        self.dimension1Step = 0
        self.dimension1Length = 1
        self.dimension2Type = 4
        self.dimension2Start = 0
        self.dimension2End = 0
        self.dimension2Step = 0
        self.dimension2Length = 1
        self.dimension3Type = 4
        self.dimension3Start = 0
        self.dimension3End = 0
        self.dimension3Step = 0
        self.dimension3Length = 1
        self.dimension4Type = 4
        self.dimension4Start = 0
        self.dimension4End = 0
        self.dimension4Step = 0
        self.dimension4Length = 1

        self.invalidNum = 0
        self.validNum = 0
        self.fMin = 0
        self.fMax = 0
        self.fMean = 0
        self.fStandardDeviation = 0
        self.percent5 = 0
        self.percent10 = 0
        self.percent25 = 0
        self.percent50 = 0
        self.percent75 = 0
        self.percent90 = 0
        self.percent95 = 0

        self.id = None
        self.wellId = None
        self.logId = None
        self.magicflag = None
        self.name = None
        self.standardName = None
        self.description = None
        self.familyName = None
        self.type = None
        self.collectionName = None
        self.moduleName = None
        self.equipment = None
        self.tools = None
        self.toolSerialNo = None
        self.toolAsset = None

        self.invalidValue = None
        self.modifyNote = None

        self.dateDimensionUnit = None

        self.dimension1Unit = None

        self.dimension2Unit = None

        self.dimension3Unit = None

        self.dimension4Unit = None
        self.reserve1 = None
        self.reserve2 = None
        self.reserve3 = None
        self.reserve4 = None
        self.reserve5 = None
        self.reserve6 = None


def create_gdx_curve(
    full_log_path: str, curve_info: BDCurveInfo, overwrite_if_exist: bool = True
) -> bool:
    """
    创建GDSX曲线并初始化其meta值。如果创建成功，返回True；否则返回False并输出警告信息

    full_log_path：GDSX文件路径
    curve_info：BDCurveInfo对象格式的meta值
    overwrite_if_exist：如果原GDSX数据中存在该曲线，是否直接覆写
    """
    f = check_gdsx_file_exist_and_return_flow(full_log_path, "r+")
    if not f:
        return False

    key = get_the_only_log_group(f)
    if not key:
        return False
    data = f[key]

    if "curve" in data.keys():
        if not isinstance(data["curve"], h5py.Group):
            warnings.warn("GDSX文件中curve键不是一个group对象，请检查您的数据！")
            return False
    else:
        data.create_group("curve")

    json_format_data = json_object2numpy(curve_info.__dict__)

    if not isinstance(curve_info.name, str):
        warnings.warn("curve_info中的name字段不是一个字符串，请检查您的数据！")
        return False

    if curve_info.name in data["curve"].keys():
        if not overwrite_if_exist:
            warnings.warn(f"GDSX文件中已含有{curve_info.name}曲线，没有完成覆盖！")
            return False

        delete_gdx_curve(full_log_path, curve_info.name)

    data["curve"].create_group(curve_info.name)
    data["curve"][curve_info.name].create_dataset("meta", data=json_format_data)

    f.close()
    return True


def delete_gdx_curve(full_log_path: str, curve_name: str) -> bool:
    """
    删除GDSX指定曲线。如果删除成功，返回True；否则返回False并输出警告信息

    full_log_path：GDSX文件路径
    curve_name：曲线名
    """
    f = check_gdsx_file_exist_and_return_flow(full_log_path, "r+")
    if not f:
        return False

    key = get_the_only_log_group(f)
    if not key:
        return False
    data = f[key]

    if "curve" in data.keys():
        if not isinstance(data["curve"], h5py.Group):
            warnings.warn("GDSX文件中curve键不是一个group对象，请检查您的数据！")
            return False
    else:
        warnings.warn("GDSX中没有curve字段，请检查您的数据！")
        return False

    if curve_name in data["curve"].keys():
        del data["curve"][curve_name]
        return True
    else:
        warnings.warn(f"GDSX中没有{curve_name}曲线，请检查您的数据！")
        return False


def get_gdx_curve_name_list(full_log_path: str) -> list[str] | None:
    """
    返回GDSX数据中所有曲线的名称。如果曲线名获取成功，返回曲线名组成的列表；否则返回None并输出警告信息

    full_log_path：GDSX文件路径
    """
    f = check_gdsx_file_exist_and_return_flow(full_log_path)
    if not f:
        return None

    list_return = []

    key = get_the_only_log_group(f)
    if not key:
        return None
    data = f[key]
    try:
        curves = data["curve"]
    except KeyError:
        warnings.warn(
            f"您的GDSX文件中{unite2str(key)}键没有curve字段，请检查您的数据！"
        )
        return None

    for curve_name in curves.keys():
        if isinstance(curve_name, str):
            list_return.append(curve_name)

    f.close()

    return list_return


def get_gdx_curve_info(full_log_path: str, curve_name: str) -> BDCurveInfo | None:
    """
    获取GDSX数据指定曲线的meta信息。如果创建成功，返回BDCurveInfo对象格式的meta值；否则返回None并输出警告信息

    full_log_path：GDSX文件路径
    curve_name：指定曲线名称
    """
    f = check_gdsx_file_exist_and_return_flow(full_log_path, "r")
    if not f:
        return None

    return_object = BDCurveInfo()

    key = get_the_only_log_group(f)
    if not key:
        return None
    data = f[key]
    try:
        curves = data["curve"]
    except KeyError:
        warnings.warn(
            f"您的GDSX文件中{unite2str(key)}键没有curve字段，请检查您的数据！"
        )
        return None

    try:
        curve = curves[curve_name]
    except KeyError:
        warnings.warn(f"您的GDSX文件中没有{curve_name}曲线，请检查您的数据！")
        return None
    except:
        warnings.warn(f"您的GDSX文件中curve字段不是一个标准的group，请检查您的数据！")
        return None

    try:
        curve_meta = curve["meta"]
    except KeyError:
        warnings.warn(f"您的{curve_name}曲线中没有meta字段，请检查您的数据！")
        return None
    except:
        warnings.warn(f"您的{curve_name}曲线不是一个标准的group，请检查您的数据！")
        return None

    json_curve = hdf5_dataset2json_object(curve_meta, curve_name)

    if (not json_curve) or (not isinstance(json_curve, dict)):
        warnings.warn(
            f"您的{curve_name}曲线信息不是一个标准的字典格式JSON字符串，请检查您的数据！"
        )
        return None

    for key, value in json_curve.items():
        if hasattr(return_object, key):  # 确保属性已存在
            setattr(return_object, key, value)
        else:
            warnings.warn(f"{key}字段在tagBDCurveInfo对象中没有定义，请检查您的数据！")

    f.close()

    return return_object


rep2number_map = {
    1: [4, "i"],
    2: [2, "h"],
    3: [4, "l"],
    4: [4, "f"],
    5: [8, "d"],
    13: [8, "q"],
}
# 1:int 2:short 3:long 4: float 5:double 13:longlong
# 值的第一个对应的是由几个字节表示一个数字，第二位对应的是struct.unpack()函数的format字符


def convert_number_list2number(number_list: list[int], rep: int) -> int | float | None:
    """将HDF5 data的一个字节流组成数字对应转换为对应的一个Python 3数字对象"""
    try:
        bytes_data = bytes(number_list)
    except:
        warnings.warn("您的数据不是标准的字节流，请检查数据！")
        return None

    try:
        return_number = struct.unpack(rep2number_map[rep][1], bytes_data)[0]
        return return_number
    except:
        warnings.warn(f"您的数据无法按照{rep}正常解析为数字，请检查数据！")


def convert_number2number_list(number: int | float, rep: int) -> list[int]:
    """将一个Python 3数字对象转换为对应的字节流数字列表"""
    byte_number = struct.pack(rep2number_map[rep][1], number)
    return list[int](byte_number)


def curveinfo2xy(curve_info: BDCurveInfo, curve_name: str):
    """根据曲线的meta信息获取其列数和维度数据"""
    npw = 1  # 曲线列数
    if curve_info.dimension < 2 or curve_info.dimension > 4:
        warnings.warn(f"{curve_name}曲线的维度设置错误，请检查您的数据！")
        return None
    if curve_info.dimension == 2:
        rep = curve_info.dimension2Type
    elif curve_info.dimension == 3:
        npw = curve_info.dimension2Length
        rep = curve_info.dimension3Type
    elif curve_info.dimension == 4:
        npw = curve_info.dimension3Length * curve_info.dimension2Length
        rep = curve_info.dimension4Type

    return (npw, rep)


def byte_number_list2number_list(
    npw: int, rep: int, data_list: list, begin_index: int, read_dep_point_count: int
) -> list[Union[int, float]] | list[list[Union[int, float]]] | None:
    """将HDF5 data的一个字节流数字列表对应转换为对应的一个Python 3数字列表"""
    if npw == 1:
        result = [
            convert_number_list2number(i, rep)
            for i in data_list[begin_index : begin_index + read_dep_point_count]
        ]
    else:
        result = []
        for point in data_list[begin_index : begin_index + read_dep_point_count]:
            one_number_byte_count = rep2number_map[rep][0]
            if not len(point) / one_number_byte_count == npw:
                warnings.warn("您的数据尺寸不正常，请检查您的数据！")
                return None

            number_lists = [
                point[i : i + one_number_byte_count]
                for i in range(0, len(point), one_number_byte_count)
            ]
            result.append([convert_number_list2number(i, rep) for i in number_lists])
    return result


def read_gdx_curve_data_by_index(
    full_log_path: str,
    curve_name: str,
    begin_index: int,
    read_dep_point_count: int,
    ignore_index: bool = True,
) -> list[Union[int, float]] | list[list[Union[int, float]]] | None:
    """
    根据曲线索引读取指定曲线数据。如果获取成功，返回列表格式的数据；否则返回None并输出警告信息

    full_log_path：GDSX文件路径
    curve_name：曲线名
    begin_index：起始索引
    read_dep_point_count：点数
    ignore_index：是否忽略索引范围检查，默认True，即不检查索引范围是否超出数据范围（这个参数是因为新疆五参13-X1常规井次MTEM曲线元信息dimension1Length比实际值要大。这是老版本LEAD导致的bug）
    """
    f = check_gdsx_file_exist_and_return_flow(full_log_path)
    if not f:
        return None

    # 如果无法获取曲线信息，直接返回None
    curve_info = get_gdx_curve_info(full_log_path, curve_name)
    if curve_info is None:
        warnings.warn(f"无法获取{curve_name}曲线的信息和数据！")
        return None

    npw_rep_tuple = curveinfo2xy(curve_info, curve_name)
    if npw_rep_tuple is None:
        return None
    else:
        npw = npw_rep_tuple[0]
        rep = npw_rep_tuple[1]

    key = get_the_only_log_group(f)
    data = f[key]
    curves = data["curve"]
    curve = curves[curve_name]

    try:
        curve_data = curve["data"]
    except KeyError:
        warnings.warn(f"您的{curve_name}曲线中没有data字段，请检查您的数据！")
        return None

    if not isinstance(curve_data[:], np.ndarray):
        warnings.warn(f"您的{curve_name}曲线数据不是一个numpy张量，请检查您的数据！")
        return None

    data_list = curve_data[:].tolist()
    if (
        begin_index < 0
        or read_dep_point_count < 1
        or begin_index + read_dep_point_count > len(data_list)
    ) and not ignore_index:
        warnings.warn("您的取数范围不符合规范，请检查！")
        return None
    result = byte_number_list2number_list(
        npw, rep, data_list, begin_index, read_dep_point_count
    )
    f.close()

    return result


def write_gdx_curve_data_by_index(
    full_log_path: str,
    curve_name: str,
    begin_index: int,
    write_dep_point_count: int,
    buffer: list[Union[int, float]] | list[list[Union[int, float]]],
) -> bool:
    """
    根据曲线索引写入指定曲线数据。如果写入成功，返回True；否则返回False并输出警告信息

    full_log_path：GDSX文件路径
    curve_name：曲线名
    begin_index：起始索引
    write_dep_point_count：点数
    buffer：写入的曲线数据
    """
    f = check_gdsx_file_exist_and_return_flow(full_log_path, "r+")
    if not f:
        return False

    curve_info = get_gdx_curve_info(full_log_path, curve_name)
    if curve_info is None:
        warnings.warn(f"无法获取{curve_name}曲线的元信息，因此无法插入曲线数据！")
        return None

    npw_rep_tuple = curveinfo2xy(curve_info, curve_name)
    if npw_rep_tuple is None:
        return False
    else:
        npw = npw_rep_tuple[0]
        rep = npw_rep_tuple[1]

    key = get_the_only_log_group(f)
    data = f[key]
    curves = data["curve"]
    curve = curves[curve_name]

    if "data" in curve.keys():
        curve_data_byte_number = curve["data"]  # 字节流数据列表
        if not isinstance(curve_data_byte_number[:], np.ndarray):
            warnings.warn(
                f"您的{curve_name}原始曲线数据不是一个numpy张量，请检查您的数据！"
            )
            return False

        data_list = curve_data_byte_number[:].tolist()
        curve_data = byte_number_list2number_list(
            npw, rep, data_list, 0, curve_info.dimension1Length
        )
        if (curve_data is None) or len(curve_data) == 0 or (curve_data[0] is None):
            warnings.warn(f"您的{curve_name}原始曲线数据解析失败！")
            return False

    else:
        curve.create_dataset(
            "data",
            (curve_info.dimension1Length, npw * rep2number_map[rep][0]),
            dtype=np.uint8,
        )
        curve_data = np.full((curve_info.dimension1Length, npw), -99999).tolist()

    if (
        begin_index < 0
        or write_dep_point_count < 1
        or begin_index + write_dep_point_count > len(curve_data)
    ):
        warnings.warn("您的数据写入范围不符合规范，请检查！")
        return False

    # 数字列表buffer直接插进去
    if not isinstance(buffer[0], list):
        buffer = [[i] for i in buffer]
    curve_data[begin_index : begin_index + write_dep_point_count] = buffer

    # 将数字列表转换为字节流数字列表
    result = []
    for point in curve_data:
        one_line = []
        for n in point:
            one_line.extend(convert_number2number_list(n, rep))
        result.append(one_line)
        
    result_numpy = np.array(result, dtype=np.uint8)
    curve["data"][...] = result_numpy
    f.close()
    return True


def rename_gdx_curve(
    full_log_path: str,
    curve_name: str,
    curve_new_name: str,
) -> bool:
    """
    重命名GDSX数据中的指定曲线。

    将旧名 `curve_name` 改为新名 `curve_new_name`，并在该曲线的 meta 的
    description 字段中记录改名信息（字典形式，形如
    {"renamed": true, "raw_name": "旧名"}）。

    成功返回 True，失败返回 False。失败（或无需改名的场景）会在以下情况返回 False：
      - 任一参数为空，或旧名 == 新名
      - 新曲线已存在
      - 原有 meta 节点不合法/无法解析

    full_log_path  ：GDSX 文件路径
    curve_name     ：当前曲线名（待重命名的旧名）
    curve_new_name ：目标新曲线名
    """
    if (
        not full_log_path
        or not curve_name
        or not curve_new_name
        or curve_name == curve_new_name
    ):
        return False

    f = check_gdsx_file_exist_and_return_flow(full_log_path, "r+")
    if not f:
        return False

    # 获取井次节点（数据根 group）
    well_log_key = get_the_only_log_group(f)
    if not well_log_key:
        f.close()
        return False
    data = f[well_log_key]

    if curve_new_name in data["curve"].keys():
        # 新曲线已存在，无需改名，直接返回
        f.close()
        return False

    # 读取原曲线的 meta 节点
    try:
        curve_group = data["curve"][curve_name]
        curve_meta = curve_group["meta"]
        json_curve = hdf5_dataset2json_object(curve_meta, curve_name)
    except (KeyError, TypeError):
        f.close()
        return False

    if (not json_curve) or (not isinstance(json_curve, dict)):
        f.close()
        return False

    # 在新 name/description/modifytime 字段上写入改名信息
    curve_info = BDCurveInfo()
    for meta_key, value in json_curve.items():
        if hasattr(curve_info, meta_key):
            setattr(curve_info, meta_key, value)

    curve_info.name = curve_new_name
    curve_info.modifytime = int(time.time() * 1000)  # 毫秒级时间戳

    # 在 description 中记录改名信息（dict: renamed/raw_name/raw_description）
    # 始终保留原始 description 值作为 "raw_description" 键，避免丢信息
    rename_note = {"renamed": True, "raw_name": curve_name}
    original_desc = curve_info.description if curve_info.description else ""
    rename_note["raw_description"] = original_desc
    curve_info.description = rename_note

    # 重新序列化 meta 并写回。
    # meta 是连续存储的定长 dataset，无法 resize；统一采用「删除再重建」保证长度可变，
    # 与 create_gdx_curve/delete_gdx_curve 的删除重建方式一致。
    meta_data = json_object2numpy(curve_info.__dict__)
    del curve_group["meta"]
    curve_group.create_dataset("meta", data=meta_data)

    # 对 HDF5 组执行重命名：data/curve/old_name -> data/curve/new_name
    # （data 为井次 group；get_the_only_log_group 返回的 key 可能是 bytes，不能用字符串拼接）
    data.move(f"curve/{curve_name}", f"curve/{curve_new_name}")

    f.close()
    return True

def parse_curve_description(desc) -> tuple[bool, Any]:
    """解析 curve 的 description。

    返回 (renamed, value/desc)：
      - renamed=True  -> value 为 dict {'renamed': True, 'raw_name': ...}（改名记录）
      - renamed=False -> value 为原 description（dict 或普通字符串，不做二次解读）
    任何异常都会回落为 (False, 原值)，绝不让脚本崩溃。
    """
    if isinstance(desc, dict):
        # 已经是 dict（可能为改名记录，也可能只是普通描述字典）
        return (desc.get("renamed") is True), desc
    if isinstance(desc, str):
        # 存储为 JSON 字符串：尝试解析，成功且为 dict 才作为改名记录识别
        try:
            parsed = json.loads(desc)
            if isinstance(parsed, dict):
                return (parsed.get("renamed") is True), parsed
        except json.JSONDecodeError:
            pass
        # 普通字符串描述（如 "原始曲线 自然伽马"），不当作改名信息
        return False, desc
    # None 或其它类型：无 description
    return False, desc

if __name__ == "__main__":
    # print(get_gdx_curve_name_list("巴64_常规大组合_20200602_1760-2740.gdsx"))
    # # 正确输出：['AC', 'AT10', 'AT20', 'AT30', ...
    curve_info = get_gdx_curve_info("巴64_常规大组合_20200602_1760-2740.gdsx", "AC")
    # print(curve_info.__dict__)
    # # 正确输出：{'majorver': 1, 'minorver': 0, 'typeNo': 268435457, 'dimension': 2...
    #
    # write_example = read_gdx_curve_data_by_index(
    #     "巴64_常规大组合_20200602_1760-2740.gdsx", "AC", 8949, 10
    # )
    # print(write_example)
    # # 正确输出：[288.9845886230469, 288.9599914550781, 288.8123779296875,...
    # # LogAI中展示的对应数据是：288.985 288.960 288.812...

    print(create_gdx_curve("GDSX例子1.gdsx", curve_info))
    curve_info2 = get_gdx_curve_info("GDSX例子1.gdsx", "AC")
    print(curve_info2.__dict__)
    # 正确输出应该跟上文的curve_info的相同

    print(delete_gdx_curve("GDSX例子1.gdsx", "AC"))
    print(create_gdx_curve("GDSX例子1.gdsx", curve_info))
    print(
        write_gdx_curve_data_by_index("GDSX例子1.gdsx", "AC", 8949, 10, write_example)
    )
    print(read_gdx_curve_data_by_index("GDSX例子1.gdsx", "AC", 8949, 10))
    # 正确输出应该跟上文的write_example的相同
