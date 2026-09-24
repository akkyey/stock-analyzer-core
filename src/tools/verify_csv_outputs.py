import pandas as pd
import polars as pl


def main() -> None:
    daily_path = "data/output/daily_report.csv"
    uncalc_path = "data/output/uncalculable_stocks.csv"

    print("=" * 80)
    print("🔍 【生成された CSV ファイルの詳細検証】")
    print("=" * 80)

    # 1. daily_report.csv の先頭行・ヘッダー確認
    print("\n--- 1. daily_report.csv ヘッダーおよび先頭行 ---")
    with open(daily_path, "r", encoding="utf-8-sig") as f:
        for i in range(4):
            print(f"Line {i+1}: {f.readline().strip()}")

    # 2. Polars によるパース
    print("\n--- 2. Polars によるパース検証 (null_values=['-']) ---")
    df_daily_pl = pl.read_csv(daily_path, null_values=["-"])
    print(f"✅ Polars パース成功: {len(df_daily_pl):,} 行, {len(df_daily_pl.columns)} 列")

    # 3. Pandas によるパース
    print("\n--- 3. Pandas によるパース検証 ---")
    df_daily_pd = pd.read_csv(daily_path)
    print(f"✅ Pandas パース成功: {len(df_daily_pd):,} 行, {len(df_daily_pd.columns)} 列")

    # 4. 契約必須カラムの欠損値チェック
    print("\n--- 4. 契約必須カラム (Code, Verdict, Score) の検証 ---")
    for col in ["Rank", "Code", "Name", "Sector", "Verdict", "Score"]:
        null_cnt = int(df_daily_pl[col].is_null().sum() or 0)
        empty_cnt = int((df_daily_pl[col].cast(pl.Utf8) == "").sum() or 0)
        hyphen_cnt = int((df_daily_pl[col].cast(pl.Utf8) == "-").sum() or 0)
        total_invalid = null_cnt + empty_cnt + hyphen_cnt
        print(
            f"  - {col:<10}: 欠損数 = {total_invalid} "
            f"(null={null_cnt}, empty={empty_cnt}, hyphen={hyphen_cnt})"
        )
        assert total_invalid == 0, f"必須列 {col} に欠損があります！"
    print("  ✅ 必須列の欠損ゼロ契約（物理保証）を完全クリア！")

    # 5. Verdict の内訳
    print("\n--- 5. Verdict 分布 ---")
    print(df_daily_pl["Verdict"].value_counts())

    # 6. uncalculable_stocks.csv の検証
    print("\n--- 6. uncalculable_stocks.csv の検証 ---")
    with open(uncalc_path, "r", encoding="utf-8-sig") as f:
        for i in range(3):
            print(f"Line {i+1}: {f.readline().strip()}")

    df_uncalc_pl = pl.read_csv(uncalc_path)
    print(f"✅ Polars パース成功: {len(df_uncalc_pl):,} 行, {len(df_uncalc_pl.columns)} 列")

    print("\n--- 7. uncalculable_stocks.csv の除外理由内訳 ---")
    if "filter_reason" in df_uncalc_pl.columns:
        print(df_uncalc_pl["filter_reason"].value_counts())
    elif "Uncalculable_Reason" in df_uncalc_pl.columns:
        print(df_uncalc_pl["Uncalculable_Reason"].value_counts())

    print("\n" + "=" * 80)
    print("🎉 全ての検証項目をパスしました。CSVは完全に正常です！")
    print("=" * 80)


if __name__ == "__main__":
    main()
