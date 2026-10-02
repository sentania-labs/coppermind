import re

with open("services/store/coppermind_store/notes.py", "r") as f:
    content = f.read()

content = content.replace("ReplaceNote,", "ReplaceNote, TagCount,")

new_method = """
    async def get_tag_counts(self) -> list[TagCount]:
        try:
            async with self.session_factory() as session:
                rows = await session.execute(
                    sa.select(
                        sa.func.unnest(Note.tags).label("tag"),
                        sa.func.count().label("count")
                    )
                    .where(Note.state != "missing")
                    .group_by("tag")
                    .order_by("tag")
                )
                return [TagCount(tag=row.tag, count=row.count) for row in rows]
        except (SQLAlchemyError, OSError) as exc:
            raise MetadataUnavailable(str(exc)) from exc
"""
content = content.replace("async def get_api_keys(self) -> ApiKeySet:", new_method.strip() + "\n\n    async def get_api_keys(self) -> ApiKeySet:")

with open("services/store/coppermind_store/notes.py", "w") as f:
    f.write(content)
