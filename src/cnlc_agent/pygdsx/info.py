import warnings

from cnlc_agent.pygdsx.unified_utils import (
    unite2str,
    hdf5_dataset2json_object,
    json_object2numpy,
    get_the_only_log_group,
    check_gdsx_file_exist_and_return_flow,
)


def get_gdx_well_info(full_log_path: str) -> dict | None:
    """
    获取GDSX数据的info的wellinfo信息。如果获取成功，返回字典格式的wellinfo信息；否则返回None并输出警告信息

    full_log_path：GDSX文件路径
    """
    f = check_gdsx_file_exist_and_return_flow(full_log_path)
    if not f:
        return None

    dict_return = {}

    key = get_the_only_log_group(f)
    if not key:
        return None
    data = f[key]
    try:
        info = data["info"]
    except KeyError:
        warnings.warn(f"您的GDSX文件中{unite2str(key)}键没有info字段，请检查您的数据！")
        return None

    try:
        wellinfo = info["wellinfo"]
    except KeyError:
        warnings.warn(
            f"您的GDSX文件中{unite2str(key)}键的info中没有weillinfo字段，请检查您的数据！"
        )
        return None
    except:
        warnings.warn(
            f"您的GDSX文件中{unite2str(key)}键的info不是一个标准的group，请检查您的数据！"
        )
        return None

    dict_return = hdf5_dataset2json_object(wellinfo, "wellinfo")

    if not isinstance(dict_return, dict):
        warnings.warn(
            "您的wellinfo不是一个标准的字典对象，将如实返回指定对象，请检查您的数据！"
        )

    if len(dict_return) == 0:
        warnings.warn(
            "您的GDSX文件中没有获取到wellinfo信息，将返回空字典。请检查您的数据！"
        )

    f.close()

    return dict_return


def set_gdx_well_info(full_log_path: str, well_info_map: dict) -> bool:
    """
    设置GDSX数据的info的wellinfo信息。如果设置成功，返回True；否则返回False并输出警告信息

    full_log_path：GDSX文件路径
    well_info_map：字典格式的wellinfo信息
    """
    if not isinstance(well_info_map, dict):
        warnings.warn("您传入的wellinfo不是一个字典对象，请检查您的数据！")
        return False

    f = check_gdsx_file_exist_and_return_flow(full_log_path, "r+")
    if not f:
        return False

    key = get_the_only_log_group(f)
    if not key:
        return False
    data = f[key]
    try:
        info = data["info"]
    except KeyError:
        info = data.create_group("info")

    numpy_data = json_object2numpy(well_info_map)
    if numpy_data is not None:
        if "wellinfo" in info.keys():
            del info["wellinfo"]
        info.create_dataset("wellinfo", data=numpy_data)

    f.close()

    return True
