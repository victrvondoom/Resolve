"""Test grouping: select with `-m <group>` (unit, api, document, cloud, voice, security, browser_e2e, windows_native)."""
import sys

GROUPS = {
    'document': ('test_document_studio', 'test_document_api', 'test_brief_mission'),
    'cloud': ('test_cloud_browser', 'test_cloud_api', 'test_sandbox_manager', 'test_connectors'),
    'voice': ('test_voice_engine', 'test_w7_voice_correction'),
    'security': ('test_production_security', 'test_tenant_security', 'test_tenant_auth', 'test_privacy_engine'),
    'browser_e2e': ('test_flagship_e2e', 'test_landing_ui', 'test_dashboard_e2e', 'test_supervisor_g5', 'test_ablations', 'test_web_g3'),
    'windows_native': ('test_windows_adapter', 'test_uia_adapter', 'test_w1_notepad', 'test_api_gui', 'test_demo_controller', 'test_demo_suite'),
    'api': ('test_brief_mission_api', 'test_web_api_g1', 'test_web_api_g5_g6', 'test_api_integration'),
}


def pytest_configure(config):
    for name in list(GROUPS) + ['unit']:
        config.addinivalue_line('markers', f'{name}: RESOLVE test group')


def pytest_collection_modifyitems(config, items):
    import pytest
    lookup = {mod: grp for grp, mods in GROUPS.items() for mod in mods}
    for item in items:
        mod = item.module.__name__.split('.')[-1]
        item.add_marker(getattr(pytest.mark, lookup.get(mod, 'unit')))


def pytest_terminal_summary(terminalreporter):
    if sys.platform != 'win32':
        terminalreporter.write_line('WINDOWS_NATIVE_TESTS_NOT_RUN: native Windows input/UIA tests need an interactive Windows session (scripts/smoke-windows.ps1).')
