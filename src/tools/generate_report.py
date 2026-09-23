import os
import sys
from pathlib import Path

import pandas as pd


def generate_html_report(csv_path: str, output_path: str):
    """CSV を DataTables 付きの HTML レポートに変換する。"""
    if not os.path.exists(csv_path):
        print(f"Error: CSV file not found at {csv_path}")
        return

    df = pd.read_csv(csv_path)

    # 簡易的な HTML テンプレート
    html_template = f"""
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Stock Analysis Report - {pd.Timestamp.now().strftime("%Y-%m-%d")}</title>
        <link rel="stylesheet" href="https://cdn.datatables.net/1.13.7/css/jquery.dataTables.min.css">
        <style>
            body {{ font-family: 'Helvetica Neue', Arial, sans-serif; margin: 20px; background-color: #f4f7f9; }}
            h1 {{ color: #2c3e50; text-align: center; }}
            .container {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
            table {{ width: 100% !important; border-collapse: collapse; }}
            th {{ background-color: #3498db !important; color: white !important; }}
            tr:nth-child(even) {{ background-color: #f9f9f9; }}
            .score-high {{ color: #27ae60; font-weight: bold; }}
            .score-low {{ color: #e74c3c; }}
        </style>
    </head>
    <body>
        <h1>📈 Stock Analysis Report</h1>
        <div class="center" style="text-align: center; margin-bottom: 20px;">
            Update: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M JST")}
        </div>
        <div class="container">
            {df.to_html(classes="display nowrap", id="analysis-table", index=False)}
        </div>

        <script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
        <script src="https://cdn.datatables.net/1.13.7/js/jquery.dataTables.min.js"></script>
        <script>
            $(document).ready(function() {{
                $('#analysis-table').DataTable({{
                    order: [[df.columns.get_loc('quant_score') if 'quant_score' in df.columns else 0, 'desc']],
                    pageLength: 50,
                    scrollX: true,
                    language: {{
                        search: "フィルター:",
                        lengthMenu: "_MENU_ 件ずつ表示",
                        info: "_TOTAL_ 銘柄中 _START_ から _END_ まで表示",
                        paginate: {{
                            first: "最初",
                            last: "最後",
                            next: "次へ",
                            previous: "前へ"
                        }}
                    }}
                }});
            }});
        </script>
    </body>
    </html>
    """

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_template)
    print(f"✅ HTML report generated: {output_path}")


if __name__ == "__main__":
    csv_file = "data/output/analysis_result.csv"
    output_file = "docs/index.html"

    # 実行場所に合わせてパスを調整
    if len(sys.argv) > 1:
        csv_file = sys.argv[1]
    if len(sys.argv) > 2:
        output_file = sys.argv[2]

    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    generate_html_report(csv_file, output_file)
