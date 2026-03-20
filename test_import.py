import sys
print("Python:", sys.version)
print("Path:", sys.path[:3])

try:
    sys.path.insert(0, '/data/Sethu/HLA-Typing-Report/venv/lib/python3.12/site-packages')
    import fitz
    print("fitz version:", fitz.__version__)
    print("SUCCESS")
except Exception as e:
    print("ERROR:", e)

with open('/data/Sethu/HLA-Typing-Report/test_import_result.txt', 'w') as f:
    f.write(f"Python: {sys.version}\n")
    try:
        import fitz
        f.write(f"fitz OK: {fitz.__version__}\n")
    except Exception as e:
        f.write(f"fitz ERROR: {e}\n")
