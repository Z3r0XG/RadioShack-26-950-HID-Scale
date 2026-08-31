"""
Backend selection tests.

hidapi builds two backends on Linux and one everywhere else, and which of them
can reach the device depends on how the machine grants access. These cover the
shapes that arise in practice without needing the corresponding platform.
"""

import types

import pytest

import read_scale as rs


def make_backend(name, *, enumerates=True, opens=True, error="Permission denied"):
    """A stand-in hidapi module that can be made to fail in specific ways."""
    module = types.ModuleType(name)

    class StubDevice:
        def open_path(self, path):
            if not opens:
                raise OSError(error)

        def read(self, size, timeout_ms=0):
            return [0xAB, 0x12, 0x13, 0x81, 0x36, 0x0D, 0xDC, 0xD0]

        def get_manufacturer_string(self):
            return "RadioShack"

        def get_product_string(self):
            return "USB Electronic Scale"

        def close(self):
            pass

    module.device = StubDevice
    module.enumerate = lambda vid, pid: (
        [{"path": name.encode(), "usage_page": 0xFF00}] if enumerates else []
    )
    return module


@pytest.fixture
def only_backends(monkeypatch):
    """
    Present exactly the named modules as importable.

    Anything else raises ImportError, so a hidapi that happens to be installed
    on the machine running the tests cannot satisfy the import instead.
    """

    def install(modules):
        def fake_import(name, *args, **kwargs):
            if name in modules:
                return modules[name]
            raise ImportError(name)

        monkeypatch.setattr(rs.importlib, "import_module", fake_import)

    return install


# --- macOS and Windows: hidapi ships the libusb-backed 'hid' module alone ---


def test_connects_through_the_only_backend(only_backends):
    only_backends({"hid": make_backend("hid")})
    assert rs.connect_scale(rs.Config()) is not None


def test_reports_why_the_only_backend_could_not_open(only_backends):
    only_backends({"hid": make_backend("hid", opens=False)})
    with pytest.raises(rs.ScaleConnectionError, match="Permission denied"):
        rs.connect_scale(rs.Config())


# --- Linux: both backends exist, and access is granted per device node ---


def test_falls_back_when_hidraw_is_blocked(only_backends):
    """A machine granting only /dev/bus/usb must still work."""
    only_backends({"hidraw": make_backend("hidraw", opens=False), "hid": make_backend("hid")})
    assert rs.connect_scale(rs.Config()) is not None


def test_falls_back_when_libusb_is_blocked(only_backends):
    """A machine granting only /dev/hidraw* must still work."""
    only_backends({"hidraw": make_backend("hidraw"), "hid": make_backend("hid", opens=False)})
    assert rs.connect_scale(rs.Config()) is not None


def test_reports_every_backend_that_failed(only_backends):
    only_backends(
        {
            "hidraw": make_backend("hidraw", opens=False, error="hidraw denied"),
            "hid": make_backend("hid", opens=False, error="libusb denied"),
        }
    )
    with pytest.raises(rs.ScaleConnectionError) as caught:
        rs.connect_scale(rs.Config())
    assert "hidraw denied" in str(caught.value)
    assert "libusb denied" in str(caught.value)


def test_missing_device_is_not_reported_as_a_permission_problem(only_backends):
    only_backends(
        {
            "hidraw": make_backend("hidraw", enumerates=False),
            "hid": make_backend("hid", enumerates=False),
        }
    )
    with pytest.raises(rs.ScaleConnectionError, match="no RadioShack 26-950"):
        rs.connect_scale(rs.Config())


# --- hidapi absent entirely ---


def test_missing_hidapi_is_a_dependency_error(only_backends):
    only_backends({})
    with pytest.raises(rs.DependencyError, match="not installed"):
        rs.connect_scale(rs.Config())


def test_a_module_without_a_device_class_does_not_count(only_backends):
    """PyPI has a different package that also imports as 'hid'."""
    only_backends({"hid": types.ModuleType("hid")})
    with pytest.raises(rs.DependencyError):
        rs.connect_scale(rs.Config())
