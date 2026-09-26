import os, warnings, h5py

from cnlc_agent.pygdsx.unified_utils import json_object2numpy


def create_gdx_log(full_log_path: str, overwrite_if_exist: bool = True) -> bool:
    """
    创建GDSX数据。如果创建成功，返回True；否则返回False并输出警告信息

    full_log_path：GDSX文件路径
    overwrite_if_exist：如果原位置存在该GDSX文件，是否直接覆写
    """
    if os.path.exists(full_log_path):
        if not overwrite_if_exist:
            warnings.warn(
                "该位置已经存在文件，您设置overwrite_if_exist为False，因此未创建新的GDSX文件！"
            )
            return False
        else:
            if os.path.isfile(full_log_path):
                try:
                    os.remove(full_log_path)
                except:
                    warnings.warn(
                        "您设置的文件路径已存在文件，且无法删除，请注意检查！"
                    )
                    return False
            else:
                warnings.warn("您设置的路径是一个文件夹，请注意检查！")
                return False

    try:
        f = h5py.File(full_log_path, "w")
    except:
        warnings.warn("您设置的文件路径无法正常新建gdsx文件，请检查您的文件管理权限！")
        return False

    gdxInfo_json = {"majorver": 1, "minorver": 1, "typeno": 0x1}

    f.create_dataset("meta", data=json_object2numpy(gdxInfo_json))

    full_name = os.path.basename(full_log_path)
    pure_name = os.path.splitext(full_name)[0]
    log_group = f.create_group(pure_name.encode("GBK"))
    log_meta = {"type": "welllog"}
    log_group.create_dataset("meta", data=json_object2numpy(log_meta))

    f.close()

    return True


if __name__ == "__main__":
    print(create_gdx_log("GDSX例子1.gdsx"))
