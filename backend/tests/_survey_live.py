"""临时脚本：节点/边/分片语料盘点，用于构造多跳评估题。"""
import sqlite3

db = sqlite3.connect('data/wl2.db')
cur = db.cursor()
print('--- confirmed nodes ---')
for r in cur.execute('select id,name from kg_node where status="confirmed" order by id'):
    print(r)
print('--- confirmed edges (named) ---')
sql = """select s.name, e.relation, d.name
         from kg_edge e join kg_node s on s.id=e.src_id join kg_node d on d.id=e.dst_id
         where e.status="confirmed" order by e.id"""
for r in cur.execute(sql):
    print(r)
print('--- active child chunks ---')
for r in cur.execute('select id,page,heading_path,text from chunk_meta where role="child" and active order by id'):
    print(r[0], r[1], r[2], '|', ' '.join((r[3] or '').split())[:70])
db.close()
