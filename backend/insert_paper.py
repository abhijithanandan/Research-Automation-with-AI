from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.session import _engine as engine
from app.models.db import PaperRow

project_id = UUID("36a0b46f-8081-4fc3-b036-4d3c4ae7d253")
with Session(engine.sync_engine) as session:
    paper = PaperRow(
        project_id=project_id,
        source="ArXiv",
        external_id="2305.12345",
        title="Human-in-the-Loop Large Language Models: A Survey",
        authors=["Alice Smith", "Bob Jones"],
        year=2023,
        abstract="This paper surveys...",
        citation_key="smith2023",
        added_at=datetime.now(UTC),
    )
    session.add(paper)
    session.commit()
    print("Paper inserted: " + str(paper.id))
