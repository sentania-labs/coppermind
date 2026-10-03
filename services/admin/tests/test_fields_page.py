from services.admin.tests.browser import admin_browser as admin_browser


def test_page_renders(signed_in):
    client, _ = signed_in
    response = client.get("/admin/fields")
    print("RESPONSE BODY:", response.text)
    assert response.status_code == 200
