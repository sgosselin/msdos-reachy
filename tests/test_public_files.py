import importlib.util
from pathlib import Path


def test_public_files_have_no_private_paths_or_credentials():
    path = Path(__file__).resolve().parents[1] / "scripts/audit_public.py"
    spec = importlib.util.spec_from_file_location("audit_public", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, findings = module.audit()
    assert findings == []
