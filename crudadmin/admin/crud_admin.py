import os
from typing import Type, Dict, Any

from fastapi import APIRouter, FastAPI, Depends
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from fastcrud import FastCRUD
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from .client.model_view import ModelView
from .client.admin_site import AdminSite
from .middleware.auth import AdminAuthMiddleware
from ..authentication.security import SecurityUtils
from ..authentication.admin_auth import AdminAuthentication
from ..db.database_config import DatabaseConfig


class CRUDAdmin:
    def __init__(
        self,
        base: DeclarativeBase,
        engine: AsyncEngine,
        session: AsyncSession,
        SECRET_KEY: str,
        mount_path: str | None = "/admin",
        theme: str | None = "dark-theme",
        ALGORITHM: str | None = "HS256",
        ACCESS_TOKEN_EXPIRE_MINUTES: int = 30,
        REFRESH_TOKEN_EXPIRE_DAYS: int = 1,
        admin_db_url: str | None = None,
        admin_db_path: str | None = None,
        db_config: DatabaseConfig | None = None,
        setup_on_initialization: bool = True,
    ) -> None:
        self.mount_path = mount_path.strip('/')
        self.theme = theme
        self.templates_directory = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "templates"
        )

        self.static_directory = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "static"
        )

        self.app = FastAPI()
        self.app.mount(
            "/static", 
            StaticFiles(directory=self.static_directory), 
            name="admin_static"
        )

        self.app.add_middleware(AdminAuthMiddleware, admin_instance=self)

        self.SECRET_KEY = SECRET_KEY
        self.ALGORITHM = ALGORITHM
        self.ACCESS_TOKEN_EXPIRE_MINUTES = ACCESS_TOKEN_EXPIRE_MINUTES
        self.REFRESH_TOKEN_EXPIRE_DAYS = REFRESH_TOKEN_EXPIRE_DAYS

        self.models: Dict[str, Dict[str, Any]] = {}
        self.router = APIRouter(tags=["admin"])
        self.oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"/{mount_path}/login")
        
        self.db_config = db_config or DatabaseConfig(
            base=base,
            engine=engine,
            session=session,
            admin_db_url=admin_db_url,
            admin_db_path=admin_db_path
        )

        self.templates = Jinja2Templates(directory=self.templates_directory)

        if setup_on_initialization:
            self.setup(
                SECRET_KEY=SECRET_KEY,
                ALGORITHM=ALGORITHM,
                ACCESS_TOKEN_EXPIRE_MINUTES=ACCESS_TOKEN_EXPIRE_MINUTES,
                REFRESH_TOKEN_EXPIRE_DAYS=REFRESH_TOKEN_EXPIRE_DAYS,
            )
        
        self.app.include_router(self.router)

    async def initialize(self):
        """Initialize the admin database tables."""
        async with self.db_config.admin_engine.begin() as conn:
            await conn.run_sync(self.db_config.AdminUser.metadata.create_all)
            await conn.run_sync(self.db_config.AdminTokenBlacklist.metadata.create_all)

    def setup(
        self,
        SECRET_KEY,
        ALGORITHM,
        ACCESS_TOKEN_EXPIRE_MINUTES,
        REFRESH_TOKEN_EXPIRE_DAYS,
    ) -> None:
        self.security_utils = SecurityUtils(
            SECRET_KEY=SECRET_KEY,
            ALGORITHM=ALGORITHM,
            ACCESS_TOKEN_EXPIRE_MINUTES=ACCESS_TOKEN_EXPIRE_MINUTES,
            REFRESH_TOKEN_EXPIRE_DAYS=REFRESH_TOKEN_EXPIRE_DAYS,
            db_config=self.db_config,
        )

        self.admin_authentication = AdminAuthentication(
            database_config=self.db_config,
            security_utils=self.security_utils,
            oauth2_scheme=self.oauth2_scheme,
        )

        self.admin_site = AdminSite(
            database_config=self.db_config,
            templates_directory=self.templates_directory,
            models=self.models,
            security_utils=self.security_utils,
            admin_authentication=self.admin_authentication,
            mount_path=self.mount_path,
            theme=self.theme,
        )

        self.admin_site.setup_routes()
        for data in self.admin_authentication.auth_models.values():
            self.add_view(
                model=data["model"],
                create_schema=data["create_schema"],
                update_schema=data["update_schema"],
                update_internal_schema=data["update_internal_schema"],
                delete_schema=data["delete_schema"],
                include_in_models=False
            )
        
        self.router.include_router(router=self.admin_site.router)

    def add_view(
        self,
        model: Type[DeclarativeBase],
        create_schema: Type[Any],
        update_schema: Type[Any],
        update_internal_schema: Type[Any],
        delete_schema: Type[Any],
        include_in_models: bool = True,
    ) -> None:
        model_key = model.__name__
        if include_in_models:
            self.models[model_key] = {
                "model": model,
                "create_schema": create_schema,
                "update_schema": update_schema,
                "update_internal_schema": update_internal_schema,
                "delete_schema": delete_schema,
                "crud": FastCRUD(model)
            }

        admin_view = ModelView(
            database_config=self.db_config,
            templates=self.templates,
            model=model,
            create_schema=create_schema,
            update_schema=update_schema,
            update_internal_schema=update_internal_schema,
            delete_schema=delete_schema,
            admin_site=self.admin_site,
        )

        router_info = {
            "router": admin_view.router,
            "prefix": f"/{model_key}",
            "include_in_schema": False,
        }

        self.app.router.include_router(dependencies=[Depends(self.admin_authentication.get_current_user())], **router_info)
