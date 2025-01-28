from datetime import timedelta, timezone, datetime
from typing import Optional, Any, Callable, Union, Dict, cast
import logging

from fastapi import Request, APIRouter, Depends, Response, Cookie
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio.session import AsyncSession

from .auth import AdminAuthentication
from .typing import RouteResponse
from ..admin_user.service import AdminUserService
from ..core.db import DatabaseConfig
from ..session.manager import SessionManager
from ..event import log_auth_action, EventType
from fastcrud import FastCRUD

logger = logging.getLogger(__name__)

EndpointCallable = Callable[..., Any]


class AdminSite:
    """
    Core admin interface site handler managing authentication, routing and views.
    """

    def __init__(
        self,
        database_config: DatabaseConfig,
        templates_directory: str,
        models: Dict[str, Any],
        admin_authentication: AdminAuthentication,
        mount_path: str,
        theme: str,
        secure_cookies: bool,
        event_integration: Optional[Any] = None,
    ) -> None:
        self.db_config = database_config
        self.router = APIRouter()
        self.templates = Jinja2Templates(directory=templates_directory)
        self.models = models
        self.admin_user_service = AdminUserService(db_config=database_config)
        self.admin_authentication = admin_authentication
        self.admin_user_service = admin_authentication.user_service
        self.token_service = admin_authentication.token_service

        self.mount_path = mount_path
        self.theme = theme
        self.event_integration = event_integration

        self.session_manager = SessionManager(
            self.db_config,
            max_sessions_per_user=5,
            session_timeout_minutes=30,
            cleanup_interval_minutes=15,
        )

        self.secure_cookies = secure_cookies

    def setup_routes(self) -> None:
        """Configure all admin interface routes including auth, dashboard and model views."""
        self.router.add_api_route(
            "/login",
            self.login_page(),
            methods=["POST"],
            include_in_schema=False,
            response_model=None,
        )
        self.router.add_api_route(
            "/logout",
            self.logout_endpoint(),
            methods=["GET"],
            include_in_schema=False,
            dependencies=[Depends(self.admin_authentication.get_current_user)],
            response_model=None,
        )
        self.router.add_api_route(
            "/login",
            self.admin_login_page(),
            methods=["GET"],
            include_in_schema=False,
            response_model=None,
        )
        self.router.add_api_route(
            "/dashboard-content",
            self.dashboard_content(),
            methods=["GET"],
            include_in_schema=False,
            dependencies=[Depends(self.admin_authentication.get_current_user)],
            response_model=None,
        )
        self.router.add_api_route(
            "/",
            self.dashboard_page(),
            methods=["GET"],
            include_in_schema=False,
            dependencies=[Depends(self.admin_authentication.get_current_user)],
            response_model=None,
        )

    def login_page(self) -> EndpointCallable:
        """
        Create login form handler for admin authentication.

        Returns route handler that processes login form, creates session and sets auth cookies.
        """

        @log_auth_action(EventType.LOGIN)
        async def login_page_inner(
            request: Request,
            response: Response,
            form_data: OAuth2PasswordRequestForm = Depends(),
            db: AsyncSession = Depends(self.db_config.get_admin_db),
            event_integration=Depends(lambda: self.event_integration),
        ) -> RouteResponse:
            logger.info("Processing login attempt...")
            try:
                user = await self.admin_user_service.authenticate_user(
                    form_data.username, form_data.password, db=db
                )
                if not user:
                    logger.warning(
                        f"Authentication failed for user: {form_data.username}"
                    )
                    return self.templates.TemplateResponse(
                        "auth/login.html",
                        {
                            "request": request,
                            "error": "Invalid credentials. Please try again.",
                            "mount_path": self.mount_path,
                            "theme": self.theme,
                        },
                    )

                request.state.user = user
                logger.info("User authenticated successfully, creating token")
                access_token_expires = timedelta(
                    minutes=self.token_service.ACCESS_TOKEN_EXPIRE_MINUTES
                )
                access_token = await self.token_service.create_access_token(
                    data={"sub": user["username"]}, expires_delta=access_token_expires
                )

                try:
                    logger.info("Creating user session...")
                    session = await self.session_manager.create_session(
                        request=request,
                        user_id=user["id"],
                        metadata={
                            "login_type": "password",
                            "username": user["username"],
                            "creation_time": datetime.now(timezone.utc).isoformat(),
                        },
                    )

                    if not session:
                        logger.error("Failed to create session")
                        raise Exception("Session creation failed")

                    logger.info(f"Session created successfully: {session.session_id}")

                    response = RedirectResponse(
                        url=f"/{self.mount_path}/", status_code=303
                    )

                    max_age_int = int(access_token_expires.total_seconds())

                    response.set_cookie(
                        key="access_token",
                        value=f"Bearer {access_token}",
                        httponly=True,
                        secure=self.secure_cookies,
                        max_age=max_age_int,
                        path=f"/{self.mount_path}",
                        samesite="lax",
                    )

                    response.set_cookie(
                        key="session_id",
                        value=session.session_id,
                        httponly=True,
                        secure=self.secure_cookies,
                        max_age=max_age_int,
                        path=f"/{self.mount_path}",
                        samesite="lax",
                    )

                    await db.commit()
                    logger.info("Login completed successfully")
                    return response

                except Exception as e:
                    logger.error(
                        f"Error during session creation: {str(e)}", exc_info=True
                    )
                    await db.rollback()
                    return self.templates.TemplateResponse(
                        "auth/login.html",
                        {
                            "request": request,
                            "error": f"Error creating session: {str(e)}",
                            "mount_path": self.mount_path,
                            "theme": self.theme,
                        },
                    )

            except Exception as e:
                logger.error(f"Error during login: {str(e)}", exc_info=True)
                return self.templates.TemplateResponse(
                    "auth/login.html",
                    {
                        "request": request,
                        "error": "An error occurred during login. Please try again.",
                        "mount_path": self.mount_path,
                        "theme": self.theme,
                    },
                )

        return cast(EndpointCallable, login_page_inner)

    def logout_endpoint(self) -> EndpointCallable:
        """
        Create logout handler for admin authentication.

        Returns route handler that terminates session and clears auth cookies.
        """

        @log_auth_action(EventType.LOGOUT)
        async def logout_endpoint_inner(
            request: Request,
            response: Response,
            db: AsyncSession = Depends(self.db_config.get_admin_db),
            access_token: Optional[str] = Cookie(None),
            session_id: Optional[str] = Cookie(None),
            event_integration=Depends(lambda: self.event_integration),
        ) -> RouteResponse:
            if access_token:
                token = (
                    access_token.replace("Bearer ", "")
                    if access_token.startswith("Bearer ")
                    else access_token
                )
                token_data = await self.token_service.verify_token(token, db)
                if token_data:
                    if "@" in token_data.username_or_email:
                        user = await self.db_config.crud_users.get(
                            db=db, email=token_data.username_or_email
                        )
                    else:
                        user = await self.db_config.crud_users.get(
                            db=db, username=token_data.username_or_email
                        )
                    if user:
                        request.state.user = user

                await self.token_service.blacklist_token(token, db)

            if session_id:
                await self.session_manager.terminate_session(
                    db=db, session_id=session_id
                )

            response = RedirectResponse(
                url=f"/{self.mount_path}/login", status_code=303
            )

            response.delete_cookie(key="access_token", path=f"/{self.mount_path}")
            response.delete_cookie(key="session_id", path=f"/{self.mount_path}")

            return response

        return cast(EndpointCallable, logout_endpoint_inner)

    def admin_login_page(self) -> EndpointCallable:
        """
        Create login page handler for admin interface.

        Returns route handler that displays login form or redirects if already authenticated.
        """

        async def admin_login_page_inner(
            request: Request,
            db: AsyncSession = Depends(self.db_config.get_admin_db),
        ) -> RouteResponse:
            try:
                access_token = request.cookies.get("access_token")
                session_id = request.cookies.get("session_id")

                if access_token and session_id:
                    token = (
                        access_token.split(" ")[1]
                        if access_token.startswith("Bearer ")
                        else access_token
                    )
                    token_data = await self.token_service.verify_token(token, db)

                    if token_data:
                        is_valid_session = await self.session_manager.validate_session(
                            db=db, session_id=session_id
                        )

                        if is_valid_session:
                            return RedirectResponse(
                                url=f"/{self.mount_path}/", status_code=303
                            )

            except Exception:
                pass

            error = request.query_params.get("error")
            return self.templates.TemplateResponse(
                "auth/login.html",
                {
                    "request": request,
                    "mount_path": self.mount_path,
                    "theme": self.theme,
                    "error": error,
                },
            )

        return cast(EndpointCallable, admin_login_page_inner)

    def dashboard_content(self) -> EndpointCallable:
        async def dashboard_content_inner(
            request: Request,
            db: AsyncSession = Depends(self.db_config.session),
        ) -> RouteResponse:
            """
            Renders partial content for the dashboard (HTMX).
            """
            context = await self.get_base_context(db)
            context.update({"request": request})
            return self.templates.TemplateResponse(
                "admin/dashboard/dashboard_content.html", context
            )

        return cast(EndpointCallable, dashboard_content_inner)

    async def get_base_context(self, db: AsyncSession) -> Dict[str, Any]:
        """Get common context data needed for base template"""
        auth_model_counts: Dict[str, int] = {}
        for model_name, model_data in self.admin_authentication.auth_models.items():
            crud_obj = cast(FastCRUD, model_data["crud"])
            if model_name == "AdminSession":
                total_count = await crud_obj.count(self.db_config.admin_session)
                active_count = await crud_obj.count(
                    self.db_config.admin_session, is_active=True
                )
                auth_model_counts[model_name] = total_count
                auth_model_counts[f"{model_name}_active"] = active_count
            else:
                count = await crud_obj.count(self.db_config.admin_session)
                auth_model_counts[model_name] = count

        model_counts: Dict[str, int] = {}
        for model_name, model_data in self.models.items():
            crud = cast(FastCRUD, model_data["crud"])
            cnt = await crud.count(db)
            model_counts[model_name] = cnt

        return {
            "auth_table_names": self.admin_authentication.auth_models.keys(),
            "table_names": self.models.keys(),
            "auth_model_counts": auth_model_counts,
            "model_counts": model_counts,
            "mount_path": self.mount_path,
            "track_events": self.event_integration is not None,
            "theme": self.theme,
        }

    def dashboard_page(self) -> EndpointCallable:
        """
        Create dashboard page handler.

        Returns route handler that displays main admin dashboard.
        """

        async def dashboard_page_inner(
            request: Request,
            db: AsyncSession = Depends(self.db_config.session),
        ) -> RouteResponse:
            context = await self.get_base_context(db)
            context.update({"request": request, "include_sidebar_and_header": True})
            return self.templates.TemplateResponse(
                "admin/dashboard/dashboard.html", context
            )

        return cast(EndpointCallable, dashboard_page_inner)

    def admin_auth_model_page(self, model_key: str) -> EndpointCallable:
        """
        Create page handler for auth model views.

        Args:
            model_key: Name of auth model to display

        Returns:
            Route handler that displays auth model list view
        """

        async def admin_auth_model_page_inner(
            request: Request,
            admin_db: AsyncSession = Depends(self.db_config.get_admin_db),
            db: AsyncSession = Depends(self.db_config.session),
        ) -> RouteResponse:
            auth_model = self.admin_authentication.auth_models[model_key]
            sqlalchemy_model = cast(Any, auth_model["model"])

            table_columns = []
            if hasattr(sqlalchemy_model, "__table__"):
                table_columns = [
                    column.key for column in sqlalchemy_model.__table__.columns
                ]

            page_str = request.query_params.get("page", "1")
            limit_str = request.query_params.get("rows-per-page-select", "10")

            try:
                page = int(page_str)
                limit = int(limit_str)
            except ValueError:
                page = 1
                limit = 10

            offset = (page - 1) * limit
            items: Dict[str, Any] = {"data": [], "total_count": 0}
            try:
                crud = cast(FastCRUD, auth_model["crud"])
                fetched = await crud.get_multi(db=admin_db, offset=offset, limit=limit)
                items = dict(fetched)

                logger.info(f"Retrieved items for {model_key}: {items}")
                total_items = items.get("total_count", 0)

                if model_key == "AdminSession":
                    formatted_items = []
                    data = items["data"]
                    for item in data:
                        if not isinstance(item, dict):
                            item = {
                                k: v
                                for k, v in vars(item).items()
                                if not k.startswith("_")
                            }
                        if "device_info" in item and isinstance(
                            item["device_info"], dict
                        ):
                            item["device_info"] = str(item["device_info"])
                        if "session_metadata" in item and isinstance(
                            item["session_metadata"], dict
                        ):
                            item["session_metadata"] = str(item["session_metadata"])
                        formatted_items.append(item)
                    items["data"] = formatted_items
            except Exception as e:
                logger.error(
                    f"Error retrieving {model_key} data: {str(e)}", exc_info=True
                )
                total_items = 0

            total_pages = max(1, (total_items + limit - 1) // limit)

            context = await self.get_base_context(db)
            context.update(
                {
                    "request": request,
                    "model_items": items["data"],
                    "model_name": model_key,
                    "table_columns": table_columns,
                    "current_page": page,
                    "rows_per_page": limit,
                    "total_items": total_items,
                    "total_pages": total_pages,
                    "primary_key_info": self.db_config.get_primary_key_info(
                        cast(Any, sqlalchemy_model)
                    ),
                    "sort_column": None,
                    "sort_order": "asc",
                    "include_sidebar_and_header": True,
                }
            )

            return self.templates.TemplateResponse("admin/model/list.html", context)

        return cast(EndpointCallable, admin_auth_model_page_inner)
