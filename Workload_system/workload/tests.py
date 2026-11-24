"""Tests for the review fixes and the academic-rank feature."""

import datetime

from django.core.exceptions import ValidationError
from django.test import Client, TestCase

from . import models, views


class BaseDataTestCase(TestCase):
    def setUp(self):
        self.ay = models.AcademicYear.objects.create(
            year_label='2026-2027', is_current=True
        )
        self.semester = self.ay.semesters.get(semester='1ST')

        self.chairman = models.FacultyProfile.objects.create(
            employee_id='CHAIR-1',
            full_name='Chair Person',
            specialization='Administration',
            attained_degree='Doctorate',
            employment_type='FULL',
            is_chairman=True,
        )
        self.faculty = models.FacultyProfile.objects.create(
            employee_id='FAC-1',
            full_name='Juan Dela Cruz',
            specialization='Mathematics',
            attained_degree="Master's Degree",
            employment_type='PART',
        )
        self.room = models.Room.objects.create(
            room_code='R-101',
            room_name='Room 101',
            floor_number=1,
            building_name='Main',
            program='BTVTED',
        )
        self.section = models.YearSection.objects.create(
            course_code='BTVTED', year_level=1, section='A',
        )
        self.subject = models.Subject.objects.create(
            year_section=self.section,
            subject_code='MATH101',
            subject_description='College Algebra',
            lecture_hours_per_week=3,
            lab_hours_per_week=0,
            faculty=self.faculty,
        )
        self.offering = models.SubjectOffering.objects.create(
            subject=self.subject, semester=self.semester,
        )

    def login(self, faculty):
        session = self.client.session
        session['faculty_id'] = faculty.employee_id
        session.save()


class ActiveFacultyMiddlewareTests(BaseDataTestCase):
    def test_deactivated_session_is_logged_out(self):
        self.faculty.is_active = False
        self.faculty.save(update_fields=['is_active'])
        self.login(self.faculty)

        response = self.client.get('/dashboard/')

        self.assertRedirects(response, '/login/')
        self.assertNotIn('faculty_id', self.client.session)

    def test_deactivated_session_does_not_redirect_loop_on_login(self):
        self.faculty.is_active = False
        self.faculty.save(update_fields=['is_active'])
        self.login(self.faculty)

        # Used to redirect to dashboard → login → dashboard … forever.
        response = self.client.get('/login/')

        self.assertEqual(response.status_code, 200)

    def test_inactive_account_cannot_reach_data_endpoints(self):
        self.faculty.is_active = False
        self.faculty.save(update_fields=['is_active'])
        self.login(self.faculty)

        response = self.client.get(
            f'/class-program-data/{self.faculty.employee_id}/'
        )

        self.assertEqual(response.status_code, 401)


class AcademicRankTests(BaseDataTestCase):
    def test_new_faculty_defaults_to_blank_academic_rank(self):
        self.assertEqual(self.faculty.academic_rank, '')

    def test_chairman_can_set_academic_rank_on_create(self):
        self.login(self.chairman)

        response = self.client.post('/faculty/create/', {
            'employee_id': 'FAC-2',
            'full_name': 'Maria Santos',
            'department': 'Technology Education',
            'specialization': 'English',
            'attained_degree': "Master's Degree",
            'academic_rank': 'Instructor I',
            'employment_type': 'PART',
            'password': 'password123',
            'confirm_password': 'password123',
            'is_active': '1',
        })

        self.assertEqual(response.status_code, 200)
        created = models.FacultyProfile.objects.get(employee_id='FAC-2')
        self.assertEqual(created.academic_rank, 'Instructor I')

    def test_academic_rank_is_optional_on_create(self):
        self.login(self.chairman)

        response = self.client.post('/faculty/create/', {
            'employee_id': 'FAC-3',
            'full_name': 'No Rank Yet',
            'department': 'Technology Education',
            'specialization': 'Science',
            'attained_degree': "Master's Degree",
            'employment_type': 'PART',
            'password': 'password123',
            'confirm_password': 'password123',
            'is_active': '1',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            models.FacultyProfile.objects.get(employee_id='FAC-3').academic_rank,
            '',
        )

    def test_chairman_can_update_academic_rank(self):
        self.login(self.chairman)

        response = self.client.post(
            f'/faculty/{self.faculty.employee_id}/update/',
            {
                'full_name': self.faculty.full_name,
                'department': 'Technology Education',
                'specialization': self.faculty.specialization,
                'attained_degree': self.faculty.attained_degree,
                'academic_rank': 'Assistant Professor II',
                'employment_type': 'PART',
                'is_active': '1',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.faculty.refresh_from_db()
        self.assertEqual(self.faculty.academic_rank, 'Assistant Professor II')

    def test_class_program_shows_academic_rank(self):
        self.faculty.academic_rank = 'Instructor III'
        self.faculty.save(update_fields=['academic_rank'])
        self.login(self.chairman)

        response = self.client.get(
            f'/class-program/{self.faculty.employee_id}/'
        )

        self.assertContains(response, 'Instructor III')

    def test_all_class_programs_json_includes_academic_rank(self):
        self.faculty.academic_rank = 'Instructor III'
        self.faculty.save(update_fields=['academic_rank'])
        self.login(self.chairman)

        response = self.client.get(
            f'/class-program-data/{self.faculty.employee_id}/'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['academic_rank'], 'Instructor III')


class OfferingResolutionTests(BaseDataTestCase):
    def test_resolve_does_not_create_by_default(self):
        offering = views._resolve_offering(self.subject, self.semester)
        self.assertIsNotNone(offering)

        other_subject = models.Subject.objects.create(
            year_section=self.section,
            subject_code='MATH102',
            subject_description='Trigonometry',
            lecture_hours_per_week=3,
            faculty=self.faculty,
        )
        self.assertIsNone(
            views._resolve_offering(other_subject, self.semester)
        )
        self.assertFalse(
            models.SubjectOffering.objects.filter(subject=other_subject).exists()
        )

    def test_resolve_creates_when_requested(self):
        other_subject = models.Subject.objects.create(
            year_section=self.section,
            subject_code='MATH103',
            subject_description='Geometry',
            lecture_hours_per_week=3,
            faculty=self.faculty,
        )

        offering = views._resolve_offering(
            other_subject, self.semester, create=True
        )

        self.assertIsNotNone(offering)
        self.assertEqual(offering.semester, self.semester)


class ClassScheduleValidationTests(BaseDataTestCase):
    def _schedule(self, day, start, end):
        return models.ClassSchedule(
            offering=self.offering,
            room=self.room,
            day=day,
            start_time=start,
            end_time=end,
            schedule_type='LECTURE',
        )

    def test_overlapping_room_booking_is_rejected(self):
        first = self._schedule(
            'MONDAY', datetime.time(8, 0), datetime.time(9, 0)
        )
        first.save()

        second = self._schedule(
            'MONDAY', datetime.time(8, 30), datetime.time(9, 30)
        )
        with self.assertRaises(ValidationError):
            second.save()

    def test_hour_limit_is_enforced(self):
        # Lecture allocation is 3h/week; two 2h blocks exceed it.
        self._schedule(
            'MONDAY', datetime.time(8, 0), datetime.time(10, 0)
        ).save()

        over = self._schedule(
            'TUESDAY', datetime.time(8, 0), datetime.time(10, 0)
        )
        with self.assertRaises(ValidationError):
            over.save()


class UtilizationAndProfileTests(BaseDataTestCase):
    def _schedule(self):
        return models.ClassSchedule.objects.create(
            offering=self.offering,
            room=self.room,
            day='MONDAY',
            start_time=datetime.time(8, 0),
            end_time=datetime.time(9, 0),
            schedule_type='LECTURE',
        )

    def test_utilization_cell_does_not_repeat_subject_code(self):
        self._schedule()
        self.login(self.chairman)

        response = self.client.get('/utilization/')

        self.assertEqual(response.status_code, 200)
        blocks = [
            cell['block']
            for grid in response.context['room_grids']
            for row in grid['rows']
            for cell in row['cells']
            if cell['type'] == 'sched'
        ]
        self.assertTrue(blocks)
        for block in blocks:
            self.assertEqual(block['section'], self.subject.section_display)
            self.assertNotIn(self.subject.subject_code, block['section'])

    def test_profile_shows_academic_rank(self):
        self.faculty.academic_rank = 'Instructor II'
        self.faculty.save(update_fields=['academic_rank'])
        self.login(self.faculty)

        response = self.client.get('/profile/')

        self.assertContains(response, 'Academic Rank')
        self.assertContains(response, 'Instructor II')


class AllClassProgramsCountTests(BaseDataTestCase):
    def test_total_classes_counts_each_schedule_once(self):
        models.ClassSchedule.objects.create(
            offering=self.offering,
            room=self.room,
            day='MONDAY',
            start_time=datetime.time(8, 0),
            end_time=datetime.time(10, 0),
            schedule_type='LECTURE',
        )
        self.login(self.chairman)

        response = self.client.get('/class-programs/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_classes'], 1)
