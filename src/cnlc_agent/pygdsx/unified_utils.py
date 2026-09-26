from typing import Any
import warnings, json, h5py

import numpy as np


def unite2str(input_str: str | bytes) -> str:
    """将字符串或UTF-8/GBK格式编码的字节流统一转换为字符串格式"""
    try:
        if isinstance(input_str, str):
            return input_str
        elif isinstance(input_str, bytes):
            try:
                return input_str.decode()  # 优先使用UTF-8解码
            except UnicodeDecodeError:
                return input_str.decode("gbk")
        else:
            warnings.warn(
                "以下内容不是字符串或GBK/UTF-8可编码的字节流，请检查：" + str(input_str)
            )
            return ""
    except UnicodeDecodeError:
        warnings.warn("以下内容不是GBK/UTF-8可编码的字节流，请检查：" + str(input_str))
        return ""


def check_gdsx_file_exist_and_return_flow(
    gdsx_path: str, mode: str = "r"
) -> h5py.File | None:
    """检查GDSX路径是否是一个GDSX文件的路径，如果是的话，返回h5py.File对象"""
    if not isinstance(gdsx_path, str):
        warnings.warn("您输入的GDSX路径不是一个字符串对象，请检查！")
        return None

    try:
        f = h5py.File(gdsx_path, mode)
        return f
    except FileNotFoundError:
        warnings.warn("无法读入GDSX文件，请检查您的路径是否正确，GDSX文件是否损坏！")
        return None


def get_the_only_log_group(f: h5py.File) -> str | bytes | None:
    """从GDSX数据中获取测井数据对应的group并返回"""
    for key in f.keys():
        if not key == "meta":
            if not isinstance(f[key], h5py.Group):
                warnings.warn("您的GDSX中井次键对应的值不是一个group，请检查您的数据！")
                return None
            return key
    warnings.warn("您的GDSX中没有井次group，请检查您的数据！")
    return None


def hdf5_dataset2json_object(
    hdf5_dataset: h5py._hl.dataset.Dataset, dataset_name: str = "wellinfo"
) -> Any | None:
    """将HDF5中dataset对象转换为JSON对象（按照GDSX的JSON对象保存理念来逆向处理）"""
    if not isinstance(hdf5_dataset[:], np.ndarray):
        warnings.warn(f"您的{dataset_name}不是一个HDF5数据集对象，请检查您的数据！")
        return None

    number_info = hdf5_dataset[:].flatten().tolist()

    try:
        bytes_info = bytes(number_info)
    except:
        warnings.warn(f"您的{dataset_name}无法正常解析为字节流，请检查您的数据！")
        return None

    str_info = unite2str(bytes_info)

    try:
        dict_return = json.loads(str_info)
    except json.decoder.JSONDecodeError:
        warnings.warn(
            f"您的{dataset_name}无法解析为JSON数据对象，请检查您的数据：" + str_info
        )
        return None

    return dict_return


def json_object2numpy(json_object: Any) -> np.ndarray | None:
    """将JSON对象转换为字符串，再转换为字节流，再转换为np.ndarray"""
    try:
        json_str = json.dumps(json_object)
    except:
        warnings.warn("以下对象无法转化为JSON字符串，请您检查数据：" + str(json_object))

    bytes_flow = json_str.encode()
    number_list = list(bytes_flow)
    return np.array(number_list, dtype=np.uint8).reshape(-1, 1)
