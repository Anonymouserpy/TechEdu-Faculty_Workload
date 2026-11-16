from django.urls import path
from . import views

urlpatterns = [
    # Authentication
    path('', views.dashboard, name='dashboard'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('register/', views.register, name='register'),
    path('login/', views.login, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Profile
    path('profile/', views.profile, name='profile'),

    # Faculty (Chairman only)
    path('faculty/', views.faculty_list, name='faculty_list'),

    # Subjects
    path('subjects/', views.all_subjects, name='all_subjects'),
    path('my-subjects/', views.my_subjects, name='my_subjects'),
    path('subject/create/', views.subject_create, name='subject_create'),
    path('subject/<int:subject_id>/edit/', views.subject_edit, name='subject_edit'),   # ← ADD

    # Rooms
    path('rooms/', views.room_list, name='room_list'),
    path('room/create/', views.room_create, name='room_create'),
    path('room/<int:room_id>/edit/', views.room_edit, name='room_edit'),
    path('room/<int:room_id>/toggle-availability/', views.room_toggle_availability, name='room_toggle_availability'),
    path('room/<int:room_id>/update-ajax/', views.room_update_ajax, name='room_update_ajax'),
    path('rooms/teachers-list/', views.rooms_teachers_list, name='rooms_teachers_list'),
    path('rooms/<int:room_id>/schedule-data/', views.room_schedule_data, name='room_schedule_data'),
    path('rooms/<int:room_id>/assign-schedule/', views.assign_room_schedule, name='assign_room_schedule'),
    path('rooms/<int:room_id>/bulk-assign/', views.assign_room_bulk, name='assign_room_bulk'),

    # Utilization
    path('utilization/', views.utilization, name='utilization'),

    # Class Schedule
    path('class-program/<str:employee_id>/assign-schedule/', views.assign_schedule, name='assign_schedule'),
    path('class-program/<str:employee_id>/bulk-assign-schedule/', views.bulk_assign_schedule, name='bulk_assign_schedule'),
    path('class-program/<str:employee_id>/delete-schedule/<int:schedule_id>/', views.delete_schedule, name='delete_schedule'),
    path('api/subjects/<str:employee_id>/', views.get_faculty_subjects, name='get_faculty_subjects'),

    # Class Programs
    path('class-program/<str:employee_id>/', views.class_program_view, name='class_program_view'),
    path('class-programs/', views.all_class_programs, name='all_class_programs'),
    path('class-program-data/<str:employee_id>/', views.class_program_data, name='class_program_data'),

    path('room-schedule/<int:schedule_id>/update/', views.room_schedule_update, name='room_schedule_update'),
    path('room-schedule/<int:schedule_id>/delete/', views.room_schedule_delete, name='room_schedule_delete'),
    path('faculty-info-data/<str:employee_id>/', views.faculty_info_data, name='faculty_info_data'),
    path('faculty/<str:employee_id>/update/', views.faculty_update, name='faculty_update'),
    path('faculty/<str:employee_id>/toggle-status/', views.faculty_toggle_status, name='faculty_toggle_status'),
    path('faculty/<str:employee_id>/delete/', views.faculty_delete, name='faculty_delete'),
    path('faculty/create/', views.faculty_create, name='faculty_create'),
        # Class List (By Section)
    path('student-program/<int:section_id>/',views.student_class_program_view, name='student_class_program_view'),
    path('class-list/', views.class_list, name='class_list'),

    path('faculty/<str:employee_id>/other-assignments/', views.other_assignment_list, name='other_assignment_list'),
    path('faculty/<str:employee_id>/other-assignments/create/', views.other_assignment_create, name='other_assignment_create'),
    path('faculty/<str:employee_id>/other-assignments/<int:assignment_id>/update/', views.other_assignment_update, name='other_assignment_update'),
    path('faculty/<str:employee_id>/other-assignments/<int:assignment_id>/delete/', views.other_assignment_delete, name='other_assignment_delete'),
    # urls.py
    path('faculty/<str:employee_id>/update-effective-date/', views.update_effective_date, name='update_effective_date'),
    path('faculty/<str:employee_id>/update-action-taken/', views.update_action_taken, name='update_action_taken'),
    path('faculty/<str:employee_id>/subject/<int:subject_id>/students/', views.update_subject_students, name='update_subject_students', ),
    path('faculty/<str:employee_id>/total-students/', views.update_total_students, name='update_total_students',),
    path('faculty/<str:employee_id>/update-remarks/', views.update_remarks, name='update_remarks',),

    path('update-effective-date/<str:employee_id>/', views.update_effective_date, name='update_effective_date'),
    path('update-term/<str:employee_id>/',            views.update_term,            name='update_term'),
    path('update-action-taken/<str:employee_id>/',    views.update_action_taken,    name='update_action_taken'),

    path('profile/change-password/', views.change_password, name='change_password'),
]
