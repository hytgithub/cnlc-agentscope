import h5py, warnings

from cnlc_agent.pygdsx.unified_utils import (
    unite2str,
    hdf5_dataset2json_object,
    json_object2numpy,
    get_the_only_log_group,
    check_gdsx_file_exist_and_return_flow,
)


def get_tables_group_from_f(f: h5py.File) -> h5py.Group | None:
    """获取GDSX数据中的tables组对象"""
    key = get_the_only_log_group(f)
    if not key:
        return None
    data = f[key]
    try:
        tables = data["table"]
    except KeyError:
        warnings.warn(
            f"您的GDSX文件中{unite2str(key)}键没有table字段，请检查您的数据！"
        )
        return None

    if not isinstance(tables, h5py.Group):
        warnings.warn("您的GDSX文件中的table键对应的值不是一个group，请检查您的数据！")
        return None

    return tables


def get_one_table_from_tables(
    tables: h5py.Group, table_name: bytes
) -> h5py.Group | None:
    """获取GDSX中的指定表格对象"""
    try:
        table = tables[table_name]
    except KeyError:
        warnings.warn(
            f"您的GDSX文件中不存在{unite2str(table_name)}表格，请检查您的数据！"
        )
        return None

    if isinstance(table, h5py.Group):
        return table
    else:
        warnings.warn(
            f"您的GDSX文件中的{unite2str(table_name)}不是一个group，请检查您的数据！"
        )
        return None


class BDTableInfo:
    def __init__(self):
        self.id = None
        self.tableName = None
        self.tableType = None
        self.tableAliasName = None
        self.owner = None
        self.catalog = None
        self.ownerId = None
        self.addDate = None
        self.reserve1 = None
        self.reserve2 = None
        self.reserve3 = None
        self.reserve4 = None
        self.reserve5 = None
        self.reserve6 = None


def create_gdsx_table(
    full_log_path: str,
    table_name: str,
    table_info: BDTableInfo,
    column_list: list[dict],
    overwrite_if_exist: bool = True,
) -> bool:
    """
    创建GDSX指定表格并初始化其meta值。如果创建成功，返回True；否则返回False并输出警告信息

    full_log_path：GDSX文件路径
    table_name：表名
    table_info：BDTableInfo对象格式的meta值（不包括columnList）
    column_list：表的列信息
    overwrite_if_exist：如果原GDSX数据中存在该表格，是否直接覆写
    """
    f = check_gdsx_file_exist_and_return_flow(full_log_path, "r+")
    if not f:
        return False

    key = get_the_only_log_group(f)
    if not key:
        return False
    data = f[key]

    if "table" not in data.keys():
        tables = data.create_group("table")
    else:
        tables = data["table"]
        if not isinstance(data["table"], h5py.Group):
            warning_content = "您的GDSX数据中，table不是一个标准的group！"
            if overwrite_if_exist:
                del data["table"]
                warnings.warn(warning_content + "已为您删除table键，并继续新建表格流程")
            else:
                warnings.warn(
                    warning_content
                    + "您设置了不覆写存在的表格数据，已为您退出新建表格流程"
                )
                return False

    bytesTableName = table_name.encode("GBK")
    try:  # 需要注意的是这里如果用if bytesTableName in tables.keys():会报UnicodeDecodeError: 'utf-8' codec can't decode byte 0xb5 in position 0: invalid start byte
        table = tables[bytesTableName]
        if overwrite_if_exist:
            if delete_gdx_table(full_log_path, table_name):
                warnings.warn(f"您的GDSX数据中存在{table_name}这个表格，已为您自动覆写")
            else:
                warnings.warn(
                    f"您的GDSX数据中存在{table_name}这个表格，但无法删除，请检查您的数据！"
                )
                return False
        else:
            warnings.warn(
                f"{table_name}已存在，您设置了不覆写存在的表格数据，已为您退出新建表格流程"
            )
            return False
    except KeyError:  # 说明本来就不存在这张表，就啥事都别干
        pass

    table = tables.create_group(bytesTableName)

    table_meta = table_info.__dict__
    table_meta["columns"] = column_list
    table.create_dataset("meta", data=json_object2numpy(table_meta))

    f.close()

    return True


def delete_gdx_table(full_log_path: str, table_name: str) -> bool:
    """
    删除GDSX指定表格。如果删除成功，返回True；否则返回False并输出警告信息

    full_log_path：GDSX文件路径
    table_name：表名
    """
    if not isinstance(table_name, str):
        warnings.warn("您输入的表名应该是一个字符串对象！")
        return False

    bytesTableName = table_name.encode("GBK")

    f = check_gdsx_file_exist_and_return_flow(full_log_path, "r+")
    if not f:
        return False

    tables = get_tables_group_from_f(f)
    if tables is None:
        return False

    try:
        del tables[bytesTableName]
    except KeyError:
        warnings.warn(f"您的GDSX文件中不存在{table_name}表格，请检查您的数据！")
        return False

    return True


def write_gdx_table_all_data(
    full_log_path: str, table_name: str, table_data: list
) -> bool:
    """
    写入GDSX指定表格的数据。如果写入成功，返回True；否则返回False并输出警告信息

    full_log_path：GDSX文件路径
    table_name：指定表名
    table_data：表格数据
    """
    if not isinstance(table_name, str):
        warnings.warn("您输入的表名应该是一个字符串对象！")
        return False

    bytesTableName = table_name.encode("GBK")

    data = json_object2numpy(table_data)
    if data is None:
        return False

    f = check_gdsx_file_exist_and_return_flow(full_log_path, "r+")
    if f is None:
        return False

    tables = get_tables_group_from_f(f)
    if tables is None:
        return False

    try:
        table = tables[bytesTableName]
        if "data" in table:
            table["data"][...] = data
        else:
            table.create_dataset("data", data=data)

    except KeyError:
        warnings.warn(f"您的GDSX文件中不存在{table_name}表格，请检查您的数据！")
        return False

    return True


def read_gdx_table_all_data(full_log_path: str, table_name: str) -> list | None:
    """
    获取GDSX指定表格数据。如果获取成功，返回list格式的数据；否则返回None并输出警告信息

    full_log_path：GDSX文件路径
    table_name：指定表名
    """
    if not isinstance(table_name, str):
        warnings.warn("您输入的表名应该是一个字符串对象！")
        return None

    bytes_table_name = table_name.encode("GBK")

    f = check_gdsx_file_exist_and_return_flow(full_log_path)
    if not f:
        return None

    tables = get_tables_group_from_f(f)
    if tables is None:
        return None

    table = get_one_table_from_tables(tables, bytes_table_name)
    if table is None:
        return None

    try:
        table_data = table["data"]
    except KeyError:
        warnings.warn(f"您的GDSX文件中不存在{table_name}表格的数据，请检查您的数据！")
        return None

    table_data = hdf5_dataset2json_object(table_data, table_name)

    f.close()
    return table_data


def get_all_gdx_table_names(full_log_path: str) -> list[str] | None:
    """
    获取GDSX数据中的所有表名。如果获取成功，返回字符串列表格式的表名；否则返回None并输出警告信息

    full_log_path：GDSX文件路径
    """
    f = check_gdsx_file_exist_and_return_flow(full_log_path)
    if not f:
        return None

    tables = get_tables_group_from_f(f)
    if tables is None:
        return None

    table_names = []
    for table_name in tables.keys():
        table_names.append(unite2str(table_name))
    f.close()
    return table_names


def get_all_gdx_table_info(full_log_path: str, table_name: str) -> BDTableInfo | None:
    """
    获取GDSX指定表格的meta信息。如果获取成功，返回BDTableInfo格式的meta信息；否则返回None并输出警告信息

    full_log_path：GDSX文件路径
    table_name：指定表名
    """
    if not isinstance(table_name, str):
        warnings.warn("您输入的表名应该是一个字符串对象！")
        return None

    bytes_table_name = table_name.encode("GBK")

    f = check_gdsx_file_exist_and_return_flow(full_log_path)
    if not f:
        return None

    tables = get_tables_group_from_f(f)
    if tables is None:
        return None

    table = get_one_table_from_tables(tables, bytes_table_name)
    if table is None:
        return None

    try:
        table_data = table["meta"]
    except KeyError:
        warnings.warn(
            f"您的GDSX文件中不存在{table_name}表格的meta信息，请检查您的数据！"
        )
        return None

    table_info = hdf5_dataset2json_object(table_data, table_name)
    if not isinstance(table_info, dict):
        warnings.warn(
            f"您的GDSX文件中{table_name}表格的meta信息不是一个字典格式的JSON字符串，请检查您的数据！"
        )
        return None

    return_object = BDTableInfo()
    for key, value in table_info.items():
        if hasattr(return_object, key):  # 确保属性已存在
            setattr(return_object, key, value)

    f.close()
    return return_object


def get_gdx_table_column_list(full_log_path: str, table_name: str) -> list[dict] | None:
    """
    创建GDSX指定表格的列信息。如果获取成功，返回列表格式的列信息；否则返回None并输出警告信息

    full_log_path：GDSX文件路径
    table_name：指定表名
    """
    if not isinstance(table_name, str):
        warnings.warn("您输入的表名应该是一个字符串对象！")
        return None

    bytes_table_name = table_name.encode("GBK")

    f = check_gdsx_file_exist_and_return_flow(full_log_path)
    if not f:
        return None

    tables = get_tables_group_from_f(f)
    if tables is None:
        return None

    table = get_one_table_from_tables(tables, bytes_table_name)
    if table is None:
        return None

    try:
        table_data = table["meta"]
    except KeyError:
        warnings.warn(
            f"您的GDSX文件中不存在{table_name}表格的meta信息，请检查您的数据！"
        )
        return None

    table_info = hdf5_dataset2json_object(table_data, table_name)
    if not isinstance(table_info, dict):
        warnings.warn(
            f"您的GDSX文件中{table_name}表格的meta信息不是一个字典格式的JSON字符串，请检查您的数据！"
        )
        return None

    f.close()
    return table_info.get("columns", [])


if __name__ == "__main__":
    table_data = read_gdx_table_all_data(
        "巴64_常规大组合_20200602_1760-2740.gdsx", "地质分层"
    )
    print(table_data)
    # 正确输出：[{'edep': '1952.900', 'layer': 'y9', 'sdep': '1924.300'},...

    print(get_all_gdx_table_names("巴64_常规大组合_20200602_1760-2740.gdsx"))
    # 正确输出：['地质分层', '油气结论', '钻井取芯']

    table_info = get_all_gdx_table_info(
        "巴64_常规大组合_20200602_1760-2740.gdsx", "地质分层"
    )
    print(table_info.__dict__)
    # 正确输出：{'id': None, 'tableName': None, 'tableType': 'GEOLAYER', 'tableAliasName': '地质分层',...

    column_list = get_gdx_table_column_list(
        "巴64_常规大组合_20200602_1760-2740.gdsx", "地质分层"
    )
    print(column_list)
    # 正确输出：[{'alias': '编号', 'allowNull': True, 'defaultValue': '',...

    print(
        create_gdsx_table("GDSX例子1.gdsx", "地质分层", table_info, column_list, True)
    )
    print(get_all_gdx_table_info("GDSX例子1.gdsx", "地质分层").__dict__)
    # 正确输出：应该和table_info相同

    print(get_gdx_table_column_list("GDSX例子1.gdsx", "地质分层"))
    # 正确输出：应该和column_list相同

    print(delete_gdx_table("GDSX例子1.gdsx", "地质分层"))
    print(
        create_gdsx_table("GDSX例子1.gdsx", "地质分层", table_info, column_list, True)
    )
    print(write_gdx_table_all_data("GDSX例子1.gdsx", "地质分层", table_data))
    print(read_gdx_table_all_data("GDSX例子1.gdsx", "地质分层"))
    # 正确输出：应该和table_data相同
