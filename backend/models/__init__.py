"""Karnex CRM ORM models (SQLAlchemy 2.0, PostgreSQL-only).

Importing this package registers every CRM table on Base.metadata —
Alembic's env.py relies on that for migrations.
"""
from models.base import Base, USERS_TABLE, USERS_FK, WorkMode  # noqa: F401
from models.masters import (  # noqa: F401
    AppSetting, CalendarYear, ContactRole, Currency, Department, Designation, DocumentType,
    FinancialYear, LeavePolicyType, Location, Skill, TaxRate, UserTablePreference
)
from models.customers import (  # noqa: F401
    BranchHolidayYear, ContactPerson, Customer, CustomerBillingPolicy, CustomerBranch, CustomerDocument,
    CustomerRateCard, CustomerStatus,
)
from models.opportunities import (  # noqa: F401
    Opportunity, OpportunityActivityLog, OpportunityApprovalStatus, OpportunityAttachment,
    OpportunityCtcSlab, OpportunitySkill, OppType, PipelineStage,
)
from models.requirements import (  # noqa: F401
    JobPostingStatus, Priority, Requirement, RequirementActivityLog, RequirementAttachment,
    RequirementJobPosting, RequirementSkill, RequirementStatus,
)
from models.candidates import (  # noqa: F401
    Candidate, CandidateEducation, CandidateExperience, CandidateSkill,
)
from models.resumes import AiInterviewStatus, AtsStatus, Resume, ResumeParseCache  # noqa: F401
from models.profiles import (  # noqa: F401
    CandidateProfile, CandidateProfileActivityLog, OfferHistory, OfferStatus, PipelineStatus,
    SkillEvaluation,
)
from models.projects import (  # noqa: F401
    BillingFrequency, BillingUnit, CommEntryType, Project, ProjectCommunicationMatrix,
    ProjectEmployee, ProjectEmployeeLeaveDetail, ProjectEmployeeRate, ProjectLeavePolicy,
    ProjectStatus,
)
from models.timesheets import (  # noqa: F401
    AttendanceStatus, DayType, EntryLocation, LeavePeriod, Timesheet, TimesheetActivityLog,
    TimesheetAttachment, TimesheetEntry, TimesheetStatus,
)
from models.finance import (  # noqa: F401
    CreditNote, CreditNoteLine, CustomerReceipt, Invoice, InvoiceLine, InvoicePayment, PaymentStatus, POActivityLog,
    POProjectAllocation, POStatus, POType, PurchaseOrder, TdsPayment, TdsRecord, TdsStatus,
)
from models.hr import (  # noqa: F401
    Employee, EmployeeEducation, EmployeeExperienceDetail, EmployeeLeaveBalance,
    EmployeeProjectHistory, ProfileType,
)
from models.leave import (  # noqa: F401
    CustomerLeavePolicy, Holiday, HolidayName, LeaveAccrualEvent, LeaveApplication, LeaveCreditConcept,
)
from models.rbac import Notification, Role, RoleName, UserRole  # noqa: F401
from models.email_outbox import EmailOutbox, EmailStatus  # noqa: F401
from models.notify_routes import ActionPermission, NotificationRoute, UserNotifyPref  # noqa: F401
from models.ai_links import AiInterviewLink  # noqa: F401
from models.scheduling import CandidateOutreach, InterviewEvent, InterviewSlot, SlotBooking  # noqa: F401
from models.template_requests import TemplateRequest, TemplateRequestStatus  # noqa: F401
from models.access_templates import AccessTemplate  # noqa: F401
from models.user_profiles import UserProfile  # noqa: F401
