"""各站点的抓取/解析逻辑按子包组织。

- clipquery.website.cgtn: CGTN 新闻（transcript / news / the-heat / world-today ...）

未来新增来源（新华社、CCTV）各自建子包，对外暴露同样的 process_one_article(url) -> dict 入口。
"""
