"""Session guards for the custom FacultyProfile authentication."""

from django.http import JsonResponse
from django.shortcuts import redirect

from . import models

# Pages a logged-out (or deactivated) visitor may still reach.
EXEMPT_PREFIXES = ('/login', '/register', '/logout', '/admin', '/static', '/media')

# Endpoints that expect JSON instead of an HTML redirect.
JSON_PREFIXES = ('/api/', '/class-program-data/', '/faculty-info-data/')


class ActiveFacultyMiddleware:
    """
    A session is only valid while its FacultyProfile exists and is active.

    Without this, deactivating (or deleting) a faculty member left any live
    session with full access, because views only checked
    ``request.session['faculty_id']``. The session is flushed here so the
    login view and dashboard can no longer bounce each other in a redirect
    loop either.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        faculty_id = request.session.get('faculty_id')

        if faculty_id and not request.path.startswith(EXEMPT_PREFIXES):
            faculty = models.FacultyProfile.objects.filter(
                employee_id=faculty_id, is_active=True
            ).first()

            if faculty is None:
                request.session.flush()
                if (
                    request.path.startswith(JSON_PREFIXES)
                    or request.headers.get('x-requested-with') == 'XMLHttpRequest'
                ):
                    return JsonResponse(
                        {'error': 'Your session is no longer valid. Please log in again.'},
                        status=401,
                    )
                return redirect('login')

            request.faculty = faculty

        return self.get_response(request)
