"""演示脚本：新建一个 GDSX 文件，创建一条曲线，随后重命名并展示改名前后的曲线名与 description 内容。

运行（在项目根目录 cnlc-agentscope 下）：
    D:\\program\\tools\\miniconda3\\envs\\agent\\python.exe pygdsx/test_rename_curve.py

@放脚本目录下运行时如遇相对导入问题，请改用：
    D:\\program\\tools\\miniconda3\\envs\\agent\\python.exe -m pygdsx.test_rename_curve
"""
import os

from cnlc_agent.pygdsx.curve import (
    BDCurveInfo,
    create_gdx_curve,
    get_gdx_curve_info,
    get_gdx_curve_name_list,
    rename_gdx_curve, parse_curve_description,
)
from cnlc_agent.pygdsx.welllog import create_gdx_log

# 输出文件（放在脚本同一目录，便于查看）
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
NEW_GDX = os.path.join(OUT_DIR, "../pygdsx/rename_demo.gdsx")


def make_curve_info(name: str) -> BDCurveInfo:
    """手工构造一条常规曲线（GR）的 meta 信息，便于演示。"""
    info = BDCurveInfo()
    info.name = name
    info.dimension = 2
    info.dimension1Type = 5  # depth: double
    info.dimension1Start = 1760.0
    info.dimension1End = 1770.0
    info.dimension1Step = 0.5
    info.dimension1Length = 20
    info.dimension2Type = 4  # data: float
    info.dimension2Length = 1
    info.description = "原始曲线 自然伽马"
    return info


def main() -> None:
    # 1. 新建一个 GDSX 文件
    # print("=" * 60)
    # print("步骤1：新建 GDSX 文件")
    # print("     路径:", NEW_GDX)
    # ok = create_gdx_log(NEW_GDX, overwrite_if_exist=True)
    # print("     创建结果:", ok)
    # assert ok, "创建 GDSX 文件失败"
    #
    # # 2. 创建一条名为 GR 的曲线
    # print("=" * 60)
    # print("步骤2：创建曲线 GR")
    # gr_info = make_curve_info("GR")
    # ok = create_gdx_curve(NEW_GDX, gr_info)
    # print("     创建结果:", ok)
    # assert ok, "创建曲线失败"
    # print("     当前曲线列表:", get_gdx_curve_name_list(NEW_GDX))
    #
    # # 3. 展示改名前的 description
    # print("=" * 60)
    # print("步骤3：重命名前 GR 的 description")
    # before = get_gdx_curve_info(NEW_GDX, "GR")
    # print("     改名前的 meta:")
    # print("       name        =", before.name)
    # print("       description =", before.description)
    #
    # # 4. 执行重命名 GR -> GR_NEW
    # print("=" * 60)
    # print("步骤4：执行重命名 GR -> GR_NEW")
    # ok = rename_gdx_curve(NEW_GDX, "GR", "GR_NEW")
    # print("     改名结果:", ok)
    # assert ok, "重命名失败"

    # 5. 展示改名后的曲线列表与 description（应含 renamed/raw_name）
    gdsx_path= r"./wplm/std/米脂3std_out.gdsx"
    # gdsx_path=r"D:\\program\\cnlc\\iLogEco\\cnlc-agentscope\\wplm\\std\\侯5std_out.gdsx"
    # print("=" * 60)
    print("改名后结果")
    print("当前曲线列表:", get_gdx_curve_name_list(r'D:\program\cnlc\gdsx_parser\gdsx\米脂3_ECLIPS-5700_常规大组合_1872-2763_20251128_完井.gdsx'))
    print("当前曲线列表:", get_gdx_curve_name_list(gdsx_path))
    for line in get_gdx_curve_name_list(gdsx_path):
        info = get_gdx_curve_info(gdsx_path, line)
        if not info:
            continue
        renamed, desc = parse_curve_description(info.description)
        if renamed:
            print(f"[已改名] {line} 的 meta:")
            print("  name        =", info.name)
            print("  raw_name    =", desc.get("raw_name"))
            print("  description =", desc)
        # elif isinstance(desc, str) and desc:
        #     print(f"[未改名-有描述] {line}: {desc}")



if __name__ == "__main__":
    main()