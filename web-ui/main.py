import os
from pathlib import Path

# Ensure templates/static are found regardless of working directory
os.chdir(Path(__file__).parent)

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import engine, Base
from app.models import User  # ensure models are registered
from app.auth.router import router as auth_router
from app.sites.router import router as sites_router
from app.api.router import router as api_router
from app.wpress.router import router as wpress_router
from app.auth.service import hash_password
from app.database import SessionLocal

app = FastAPI(title="EZWeb")
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth_router)
app.include_router(sites_router)
app.include_router(api_router)
app.include_router(wpress_router)


@app.on_event("startup")
def startup():
    from app.models import PortReservation
    Base.metadata.create_all(bind=engine)

    from app import migrations
    migrations.run_pending_migrations()

    db = SessionLocal()
    try:
        # Create default admin if no users exist
        if db.query(User).count() == 0:
            admin = User(
                username="admin",
                password_hash=hash_password("admin"),
                is_admin=True,
            )
            db.add(admin)
            db.commit()
            print("[OK] Default admin created -- username: admin  password: admin")
            print("[!]  Please change the password immediately!")

        # Clean up sites stuck in creating/deleting from a previous crashed run
        from app.models import Site
        stale = db.query(Site).filter(Site.status.in_(["creating", "deleting"])).all()
        for s in stale:
            db.query(PortReservation).filter(
                PortReservation.site_id == s.id
            ).update({"status": "released"})
            db.delete(s)
            print(f"[cleanup] Removed stale site: {s.id}")
        if stale:
            db.commit()
    finally:
        db.close()

    from app.wpress.service import cleanup_partial_uploads
    if n := cleanup_partial_uploads():
        print(f"[cleanup] Removed {n} interrupted template upload(s)")
