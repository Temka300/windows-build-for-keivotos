"""Disposable Home state changes; never installed in a normal application."""
import hashlib
import os
from pathlib import Path


def install(media):
    from fastapi import Request
    from fastapi.routing import APIRoute
    from server import app
    from database import get_data_db, get_user_db
    from files_base import sources

    home = Path(os.environ["KEIVOTOS_HOME"])
    assert home.name == "home" and home.parent.name.startswith("keivotos-modularization-")
    artwork = home / "artwork"
    original_bytes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in artwork.glob("*.jpg")}
    assert len(original_bytes) == 48
    tables = ("files", "tags", "posts", "post_tags")
    original = None

    def snapshot():
        nonlocal original
        if original is None:
            with get_data_db() as connection:
                original = {table: connection.execute("SELECT * FROM " + table).fetchall() for table in tables}

    def proof():
        snapshot()
        with get_data_db() as connection:
            counts = {table: connection.execute("SELECT COUNT(*) AS n FROM " + table).fetchone()["n"] for table in tables}
        current = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in artwork.glob("*.jpg")}
        return {"counts": counts, "media_preserved": current == original_bytes, "media_files": len(current)}

    async def change(request: Request):
        snapshot()
        action = (await request.json())["action"]
        assert action in ("empty", "restore", "small", "untagged", "register")
        if action != "register":
            with get_data_db() as connection:
                for table in reversed(tables):
                    connection.execute("DELETE FROM " + table)
                if action != "empty":
                    for table in tables:
                        for row in original[table]:
                            columns = list(row)
                            connection.execute("INSERT INTO " + table + " (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", list(row.values()))
                    if action == "small":
                        connection.execute("DELETE FROM post_tags WHERE post_id<>1")
                        connection.execute("DELETE FROM posts WHERE id<>1")
                        connection.execute("DELETE FROM files WHERE id<>1")
                        connection.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM post_tags)")
                    if action == "untagged":
                        connection.execute("DELETE FROM post_tags")
                        connection.execute("DELETE FROM tags")
                connection.commit()
        if action == "untagged":
            with get_user_db() as connection:
                row = original["files"][0]
                connection.execute("INSERT OR IGNORE INTO favorites(file_id,file_path,local_md5) VALUES(?,?,?)", (row["id"], row["path"], row["local_md5"]))
                connection.commit()
        if action == "register":
            with get_user_db() as connection:
                connection.execute("INSERT OR REPLACE INTO registered_folders(name,path,root_id,display_name) VALUES('home-fixture-root',?,'home-fixture-root','Fixture artwork')", (str(artwork),))
                source = sources.get_source_by_path(connection, str(artwork)) or sources.register_source(connection, artwork, "Fixture artwork")
                sources.update_source(connection, source.source_id, role="danbooru", commit=False)
                connection.commit()
            with get_data_db() as connection:
                for row in original["files"]:
                    connection.execute("UPDATE files SET root_id='home-fixture-root',relative_path=?,folder='Fixture artwork' WHERE id=?", (row["name"], row["id"]))
                connection.commit()
        return proof()

    for name, callback, methods in (("proof", proof, ["GET"]), ("change", change, ["POST"])):
        app.router.routes.insert(0, APIRoute("/api/fixture/home/" + name, callback, methods=methods))
