"""When optional third-party packages are not installed (offline dev box), install minimal
stand-ins so pure-logic tests can import metrics. Real CI installs the real packages."""
import sys
import types


def install_prometheus_stub_if_missing():
    try:
        import prometheus_client  # noqa: F401
        return False
    except ImportError:
        pass

    class _Metric:
        def __init__(self, *a, **k): pass
        def labels(self, *a, **k): return self
        def inc(self, *a, **k): pass
        def dec(self, *a, **k): pass
        def observe(self, *a, **k): pass

    mod = types.ModuleType("prometheus_client")
    mod.Counter = mod.Gauge = mod.Histogram = _Metric
    sys.modules["prometheus_client"] = mod
    return True
