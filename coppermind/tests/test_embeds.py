from coppermind.embeds import rewrite_embeds

FILED = "_Attachments/2026-09/SRC1-r0001-x-image-01.jpg"
IMAGES = [("SRC1", "x-image-01.jpg", FILED)]


def rewrite(body: str, images=IMAGES) -> str:
    return rewrite_embeds(body, images, attachments_folder="_Attachments")


def test_wikilink_and_markdown_embeds_name_an_artifact():
    assert rewrite("![[x-image-01.jpg]]\n![](x-image-01.jpg)\n![alt](x-image-01.jpg)") == (
        f"![[{FILED}]]\n![]({FILED})\n![alt]({FILED})"
    )


def test_alias_is_kept_and_unmatched_links_are_untouched():
    body = "![[x-image-01.jpg|300]] ![[other.jpg]] ![](y.jpg) [[x-image-01.jpg]] ![](http://h/x-image-01.jpg)"
    assert rewrite(body) == body.replace("![[x-image-01.jpg|300]]", f"![[{FILED}|300]]")


def test_no_images_leaves_body_alone():
    assert rewrite("![[x-image-01.jpg]]", []) == "![[x-image-01.jpg]]"


def test_rewriting_twice_changes_nothing():
    once = rewrite("![[x-image-01.jpg]] ![](x-image-01.jpg)")
    assert rewrite(once) == once


def test_new_revision_repoints_earlier_filed_links():
    current = [("SRC1", "x-image-01.jpg", "_Attachments/2026-09/SRC1-r0002-x-image-01.jpg")]
    assert rewrite(f"![[{FILED}]] ![]({FILED})", current) == (
        "![[_Attachments/2026-09/SRC1-r0002-x-image-01.jpg]] "
        "![](_Attachments/2026-09/SRC1-r0002-x-image-01.jpg)"
    )


def test_other_sources_filed_copies_are_not_repointed():
    other = "_Attachments/2026-09/SRC2-r0001-x-image-01.jpg"
    assert rewrite(f"![[{other}]]") == f"![[{other}]]"


def test_spaces_are_encoded_only_in_markdown_form():
    filed = "_Attachments/2026-09/SRC1-r0001-my photo.jpg"
    images = [("SRC1", "my photo.jpg", filed)]
    assert rewrite("![[my photo.jpg]]", images) == f"![[{filed}]]"
    assert (
        rewrite("![](my%20photo.jpg)", images)
        == "![](_Attachments/2026-09/SRC1-r0001-my%20photo.jpg)"
    )


def test_fenced_code_is_not_rewritten():
    body = "```\n![[x-image-01.jpg]]\n```\n![[x-image-01.jpg]]\n"
    assert rewrite(body) == f"```\n![[x-image-01.jpg]]\n```\n![[{FILED}]]\n"
