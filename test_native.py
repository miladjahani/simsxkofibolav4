from app.native_engine import NativeWorkbook
p='app/data/model.xlsx'
r=NativeWorkbook(p).calculate('2Ex1S A1', only_cells=['B7','B8','B9','D5','D6'])
print(r['values'])
