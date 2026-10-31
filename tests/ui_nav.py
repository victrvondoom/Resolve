"""Shared helper for browser tests: open a view through the sidebar the way a user does.
The sidebar has two workspace areas (Workbench, Operations); the helper opens the area that owns the view, then clicks its link."""
OPERATIONS = {'morning', 'inbox', 'thread', 'outbox', 'commitments', 'commitment', 'drafts', 'draft', 'approvals', 'activity',
              'rules', 'connections', 'replay', 'privacy', 'settings', 'connectors'}


def click_nav(page, view: str) -> None:
    area = 'operations' if view in OPERATIONS else 'workbench'
    tab = page.locator(f'[data-area-btn="{area}"]')
    if tab.get_attribute('aria-selected') != 'true':
        tab.click()
    page.locator(f'.sidebar a[data-view="{view}"]').click()
