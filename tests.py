\
import os, tarfile, zipfile, io, tempfile, shutil
from xtrak.scanner import scan_directory, is_probably_archive

def make_sample_tree(root):
    # Create files
    with open(os.path.join(root, "note.txt"), "w") as f:
        f.write("hello")

    # zip with nested tar.gz
    zip_path = os.path.join(root, "outer.zip")
    nested_tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=nested_tar_bytes, mode="w:gz") as tf:
        ti = tarfile.TarInfo("inside.txt")
        data = b"data"
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    nested_tar_bytes.seek(0)

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("a.txt", "a")
        zf.writestr("nested.tar.gz", nested_tar_bytes.read())

    # tar with nested zip
    tar_path = os.path.join(root, "bundle.tar")
    nested_zip_bytes = io.BytesIO()
    with zipfile.ZipFile(nested_zip_bytes, "w") as zf:
        zf.writestr("deep.txt", "deep")
    nested_zip_bytes.seek(0)

    with tarfile.open(tar_path, "w") as tf:
        ti = tarfile.TarInfo("inner.zip")
        data = nested_zip_bytes.getvalue()
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))

def test_scan_directory_recursive():
    with tempfile.TemporaryDirectory() as d:
        make_sample_tree(d)
        findings = scan_directory(d, recurse_archives=True, max_depth=3)
        exts = [f.ext for f in findings]
        assert "zip" in exts
        assert "tar" in exts or "tar.gz" in exts
        # nested detections
        assert any("nested.tar.gz" in f.path for f in findings)
        assert any("inner.zip" in f.path for f in findings)

def test_is_probably_archive():
    assert is_probably_archive("data.tar.gz")[0]
    assert is_probably_archive("photo.tgz")[0]
    assert is_probably_archive("file.zip")[0]
    assert is_probably_archive("file.7z")[0]
    assert not is_probably_archive("note.txt")[0]
