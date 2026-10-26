from fastapi import APIRouter

from app.api.v1 import auth, documents, graph, knowledge_bases, notes, qa, search, system

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(knowledge_bases.router)
api_router.include_router(notes.router)
api_router.include_router(documents.router)
api_router.include_router(graph.router)
api_router.include_router(search.router)
api_router.include_router(qa.router)
