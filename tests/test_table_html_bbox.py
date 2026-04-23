from bs4 import BeautifulSoup


def parse_html_table(html_table_str):
    """
    解析 HTML 表格字符串，提取 行数、列数、单元格数据
    :param html_table_str: 包含 <table><tr><td> 的字符串
    :return: rows(行数), cols(列数), table_data(二维数据)
    """
    soup = BeautifulSoup(html_table_str, "html.parser")
    table = soup.find("table")
    rows_data = []

    # 遍历所有行 <tr>
    for tr in table.find_all("tr"):
        row = []
        # 遍历所有单元格 <td>
        for td in tr.find_all("td"):
            row.append(td.get_text(strip=True))  # 提取单元格文本
        if row:
            rows_data.append(row)

    if not rows_data:
        return 0, 0, []

    rows = len(rows_data)
    cols = max(len(row) for row in rows_data)  # 取最大列数
    return rows, cols, rows_data


def get_table_cells_from_html(table_bbox, html_table_str):
    """
    从 HTML 表格字符串 + 表格整体bbox，计算所有单元格的 bbox 和数据
    """
    # 1. 解析 HTML 得到行数、列数、数据
    rows, cols, table_data = parse_html_table(html_table_str)

    # 2. 拆解表格 bbox
    x1_table, y1_table, x2_table, y2_table = table_bbox
    table_w = x2_table - x1_table
    table_h = y2_table - y1_table

    # 3. 单元格宽高
    cell_w = table_w / cols
    cell_h = table_h / rows

    cells = []

    # 4. 遍历每个单元格
    for row_idx in range(rows):
        for col_idx in range(cols):
            # 计算 bbox
            x1 = x1_table + col_idx * cell_w
            y1 = y1_table + row_idx * cell_h
            x2 = x1 + cell_w
            y2 = y1 + cell_h

            # 单元格数据
            try:
                data = table_data[row_idx][col_idx]
            except IndexError:
                data = ""

            cells.append({
                "row": row_idx + 1,
                "col": col_idx + 1,
                "bbox": [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
                "data": data
            })

    return cells


if __name__ == '__main__':
    # 1. 表格整体 bbox
    table_bbox = [ 113,
            123,
            428,
            890]
    # 2. 你的 HTML 表格字符串（包含 tr td）
    html_table = """
   <table>
        <tr>
            <td></td>
            <td>Area (ha)</td>
            <td>Species</td>
            <td>% Area planted</td>
            <td>Mean annual rainfall (mm)</td>
            <td>*Rain distrib.</td>
            <td>Pre-treatment (years)</td>
            <td>Forest age (years)</td>
            <td>Mean soil depth (m)</td>
            <td>BFI</td>
            <td>Key references</td>
        </tr><tr><td>Traralgon Ck (Vic)</td><td>8700</td><td>Eucalypt</td><td>~70</td><td>1472</td><td>U</td><td>22</td><td>19</td><td>2.0</td><td>0.37</td><td></td></tr><tr><td>Redhill (NSW)</td><td>195</td><td>P. radiata</td><td>78</td><td>866</td><td>W</td><td>0</td><td>9</td><td>1.0</td><td>0.39</td><td>Hickel, 2001</td></tr><tr><td>Pine Ck (Vic)</td><td>320</td><td>P. radiata</td><td>100</td><td>775</td><td>W</td><td>0</td><td>11</td><td>&lt;1.0</td><td>0.26</td><td></td></tr><tr><td>Stewarts Ck 5 (Vic)</td><td>18</td><td>P. radiata</td><td>100</td><td>1156</td><td>W</td><td>9</td><td>20</td><td>&lt;1.0</td><td>0.28</td><td>Nandakumar and Mein, 1993</td></tr><tr><td>Glendhu 2 (NZ)</td><td>310</td><td>P. radiata</td><td>67</td><td>1282</td><td>U</td><td>3</td><td>17</td><td>1.0</td><td>0.64</td><td>Fahey and Jackson, 1997</td></tr><tr><td>Cathedral Peak 2 (SA)</td><td>190</td><td>P. patula</td><td>75</td><td>1436</td><td>S</td><td>2</td><td>20</td><td>1.5–2.0</td><td>0.66</td><td>Scott et al., 2000</td></tr><tr><td>Cathedral Peak 3 (SA)</td><td>139</td><td>P. patula</td><td>86</td><td>1504</td><td>S</td><td>6</td><td>17</td><td>1.5–2.0</td><td>0.75</td><td>Scott et al., 2000</td></tr><tr><td>Lambrechtbos A (SA)</td><td>31</td><td>P. radiata</td><td>82</td><td>1134</td><td>W</td><td>30</td><td>19</td><td>1.5–2.0</td><td>0.78</td><td>Scott et al., 2000</td></tr><tr><td>Lambrechtbos B (SA)</td><td>66</td><td>P. radiata</td><td>89</td><td>1088</td><td>W</td><td>17</td><td>20</td><td>1.5–2.0</td><td>0.87</td><td>Scott et al., 2000</td></tr><tr><td>Biesievlei (SA)</td><td>27</td><td>P. radiata</td><td>98</td><td>1332</td><td>W</td><td>10</td><td>20</td><td>1.5–2.0</td><td>0.72</td><td>Scott et al., 2000</td></tr></table>
    """

    # 3. 直接调用
    cells = get_table_cells_from_html(table_bbox, html_table)

    # 4. 打印结果
    for cell in cells:
        print(cell)