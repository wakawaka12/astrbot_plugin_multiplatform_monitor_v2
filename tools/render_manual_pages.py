import os
import pypdfium2 as pdfium

pdf_path = r"D:\监控\astrbot_plugin_multiplatform_monitor_v2\assets\docs\game_member_manual.pdf"
out_dir = r"D:\监控\astrbot_plugin_multiplatform_monitor_v2\assets\docs\manual_pages"
os.makedirs(out_dir, exist_ok=True)
# clear old
for fn in os.listdir(out_dir):
    if fn.endswith(".png"):
        os.remove(os.path.join(out_dir, fn))

doc = pdfium.PdfDocument(pdf_path)
print("pages", len(doc))
scale = 2.0  # ~144 dpi
for i in range(len(doc)):
    page = doc[i]
    bitmap = page.render(scale=scale)
    pil = bitmap.to_pil()
    # ensure reasonable width
    if pil.width > 1600:
        ratio = 1600 / pil.width
        pil = pil.resize((1600, int(pil.height * ratio)))
    out = os.path.join(out_dir, f"page_{i+1:02d}.png")
    pil.save(out, "PNG", optimize=True)
    print("saved", out, pil.size, os.path.getsize(out))
doc.close()
