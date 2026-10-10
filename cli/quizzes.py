from __future__ import annotations

from typing import Annotated, Callable

import typer

quizzes_app = typer.Typer(help="Quiz commands (read-only; never opens or starts a quiz).")


def register(invoke: Callable[[str, dict], None]) -> typer.Typer:
    @quizzes_app.command("list")
    def quizzes_list(
        course_id: Annotated[str, typer.Argument(help="Course ID.")],
        limit: Annotated[int, typer.Option(help="Maximum quizzes.")] = 100,
    ) -> None:
        invoke("list_course_quizzes", {"course_id": course_id, "limit": limit})

    @quizzes_app.command("results")
    def quizzes_results(
        course_id: Annotated[str, typer.Argument(help="Course ID.")],
        quiz_id: Annotated[str, typer.Argument(help="Quiz ID.")],
        attempt: Annotated[int | None, typer.Option(help="Attempt number; defaults to the latest.")] = None,
    ) -> None:
        args: dict = {"course_id": course_id, "quiz_id": quiz_id}
        if attempt is not None:
            args["attempt"] = attempt
        invoke("get_quiz_results", args)

    return quizzes_app
