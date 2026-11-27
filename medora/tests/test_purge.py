"""Regression: startup cleanup of old demo accounts must not crash the app.

On Vercel/Postgres, deleting an old demo account that still had a login session raised
auth_sessions_account_id_fkey during startup, and every request returned 500.
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import Base
from app.models import Account, AuthSession, Entry, Patient


def _fk_engine(tmp_path):
    eng = create_engine(f"sqlite:///{(tmp_path / 'purge.db').as_posix()}")

    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _):  # enforce foreign keys the way Postgres does
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    return eng


def test_old_demo_with_live_session_is_purged_without_error(tmp_path, monkeypatch):
    eng = _fk_engine(tmp_path)
    Session = sessionmaker(bind=eng, expire_on_commit=False)
    old = datetime.now() - timedelta(days=5)
    with Session() as s:
        keep = Account(login="real@example.com", is_demo=False, created_at=old)
        for i in range(3):  # several old demo accounts, so the loop flushes between them
            acc = Account(login=f"demo{i}@sample", is_demo=True, created_at=old)
            s.add(acc)
            s.flush()
            p = Patient(account_id=acc.id, name=f"Demo {i}")
            s.add(p)
            s.flush()
            s.add(Entry(patient_id=p.id, name="Dolo 650", ingredients=[]))
            s.add(AuthSession(token_hash=f"t{i}", account_id=acc.id, patient_id=p.id,
                              expires_at=datetime.now() + timedelta(days=30)))
        s.add(keep)
        s.commit()

    monkeypatch.setattr(main, "SessionLocal", Session)
    main._purge_old_demos()  # raw version: must succeed, not just be swallowed

    with Session() as s:
        assert [a.login for a in s.scalars(select(Account))] == ["real@example.com"]
        assert list(s.scalars(select(AuthSession))) == []
        assert list(s.scalars(select(Patient))) == []
        assert list(s.scalars(select(Entry))) == []


def test_cleanup_failure_never_stops_startup(monkeypatch):
    def boom():
        raise RuntimeError("database said no")

    monkeypatch.setattr(main, "_purge_old_demos", boom)
    main.purge_old_demos()  # must not raise
