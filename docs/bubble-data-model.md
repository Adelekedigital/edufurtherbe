**BRAIN DUMP FOR EDUFURTHER MIGRATION FROM BUBBLE TO FULL STACK**

The new build direction will require building the frontend and backend as separate project for easier maintenance and build. Starting with the backend that will be reference as BE.**DATA MODEL FROM BUBBLE TO BE**

The current data model in bubble will be shared and we wlll see how we can better refine, improve and present the model in the new BE architecture without loosing important data reference from bubble. Highlevel data model below in list format with each with it’s payload schema.

This document shares the current shape of the legacy app data. Going through this migration, lot’s of clean up will need to be done to build for scale. It’s a deep discussion on handling this. Lot’s of decisions taken for this data models where base on bubble limitations and the needed work around to by pass the limitations. Also early phase of this project build was handled by my inexperience self. Some data decision I made I can’t repeat them at my current level and experience like over filling the user table.Number value beside each table name are the current database value count.

*   User: table storing all users >> (1200)

    *   Admin >> Admin-role option set( Super Admin, Mentor Approval, Limited access)

    *   bookingCredit

    *   bookingCreditRenewDate

    *   Calendar integration via composio

        *   calAccessToken

        *   calAccessTokenExpiresAt

        *   calClientId

        *   calDefaultScheduleId

        *   calEventId

        *   calRefreshToken

        *   calRefreshTokenExpiresAt

        *   composioAuthId

    *   email verified date

    *   emailitContact\_id

    *   First and Last Name

    *   First Name

    *   Last Active

    *   Last Name

    *   member goal >> link back to member goal table

    *   Mentor >> link back to mentor table

    *   mentor service >> link back to mentor service

    *   New password change date

    *   password reset confirm

    *   registration completed

    *   Registration completed (Y/N)

    *   Registration format >> Registration format option set(Email, Google, Linkedin)

    *   Terms agreed date

    *   User-last-onboarding-step

    *   UserTimezonID

    *   User Profile image

    *   Personal Info >> link back to personalinfo table

    *   Role >> user role option set(mentee, mentor)

    *   Interest >> link to interest table(but i think it was deprecated, there might be reason to bring it back)

    *   Education >> link to list of eduction rows

    *   Email


*   SessionBooking (member): Table storing all session booked on the platform >> (1,073)

    *   bookingRequestAccepted

    *   Canceled By

    *   datePicked

    *   datePickedText

    *   Duration

    *   google/dailyMeetingVenue

    *   google/dailyRoomName

    *   googleCalEventId

    *   Meeting venue

    *   Session Cancel/Decline Message

    *   Session Initiator >> link to the mentee’s user table 

    *   Session topic

    *   SessionCancel (Y/N)

    *   SessionDateTime-UTC(Booked date)

    *   sessionStatus

    *   slotBookedTime

    *   trackedSessionPosthog(Y/N)

    *   Weekday(number)

    *   Session booking Message

    *   Mentor >> link to the mentor’s user table

    *   Creator


*   SessionTracker: Table storing all session tracking >> (935)

    *   Canceled

    *   Expiration

    *   google/dailyMeetingVenue

    *   google/dailyRoomName

    *   Last Joined(mentee)

    *   Last Joined(Mentor)

    *   Meetinglink

    *   Mentee(userdatatype)

    *   Mentor(this session)

    *   Mentor(userdatatype)

    *   Session Duration

    *   session time

    *   SessionID >> link back to SessionBooking (member)

    *   sessionTrackedPosthog

    *   TrackID

    *   TrackStatus(mentee)

    *   TrackStatus(Mentor)


*   Members Goals: Table for storing mentees goals >> (720)

    *   completedSession

    *   Country Goal >> list of country target base on country list api

    *   degreeGoal(text)

    *   Mentorship Goals(Text) >> list of mentorship goals base on option set value

*   Mentor Services: Table for storing mentors services >> (31)

    *   Mentor Services/Support(text) >> list of mentor services/support base on option set value

    *   Scholarship Experience >> list of mentor scholarship Experience base on option set value


*   Reviews: Table storing mentors reviews >> (53)

    *   communicationRating

    *   knowledgeRating

    *   likertValuableRating

    *   npsRecommendScore

    *   practicalityRating

    *   privateReview

    *   publicReview

    *   reviewedBy

    *   reviewedFor

    *   supportRating


*   Scholarship-Awards: Table storing scholarship awards earned and provided by the mentor  >> (17)

    *   Award-institution

    *   Award-title

    *   Award-year


*   PersonalInfo: Table for storing personal info of users >> (858)

    *    Profile banner Image

    *   About me

    *   Country of Origin

    *   Country of study(mentor)

    *   Gender

    *   Language

    *   list-Language

    *   OriginCountry(text)

    *   Social Linkedin

    *   Social Twitter

    *   Social Youtube

    *   StudyCountry(text)


*   CalendarSettings: Table for storing mentors calendar availability >> (192)

    *   12hr-localEndTime-TXT

    *   12hr-localStartTime-TXT

    *   24hr-localEndTime-TXT

    *   24hr-locatStartTime-TXT

    *   availableDay-Bool

    *   dayOfWeekIn

    *   daysOfWeek-O/S >> option set value

    *   endTime

    *   meetingDuration-TxT

    *   meetingVenue

    *   startTime

    *   timeZone


*   Education: Table for storing users education information >> (940)

    *   dateEnd

    *   dateStart

    *   degreeCategory

    *   mostRecentDegree >> bool

    *   schoolName

    *   shortForm

    *   studyCourse

    *   studyFieldInsterest >> option set value

    *   studyProgram-O/S  >> option set value


*   Mentor (front search): Table for storing mentor user for easy access for mentees. It was initially created as workaround depicting the bubble community satellite database for storing data to be easily accessible by users to avoid over querying the main data like users, education etc. But it’s became bloated >> (44 data)

    *   availableStatus

    *   confirmationRequired

    *   countCompletedSession

    *   countReviewReceived

    *   countryOrigin

    *   degreeCategory

    *   Gender

    *   meetingDuration

    *   meetingVenueSelection >> optin set value

    *   mentorLanguages >> list of languages

    *   mentorMentorshipSupport(listText)

    *   mentorServices >> link to mentorservices table

    *   percentageOfCompletedSession

    *   pictureProfile

    *   statusApproved-DeclinedDate

    *   unavailableDateRange

    *   unavailableDuration

    *   approvedText >> bool for accepting a mentors application and it’s use to confirm if a mentor can be made available for listing

    *   studyCountry

    *   studyCourse

    *   nameFirstLast

    *   latestUniversity

    *   studyProgram

    *   Education >> list of education row

    *   meetingVenueLink 


*   Notifications: Table for storing platform supposed notification system. We might have  abetter bet exploring thirdparty notification solution especially since this build will be heavy on PWA. I am opening to exploring open-source options that would cut doen cost. >> (681)

    *   Notification Sender body

    *   Notification Title

    *   Notify Type

    *   Receiver

    *   Receiver(list of users)

    *   Review

    *   Seen(list of users)

    *   Session

    *   Seen receiver

    *   Seen sender


*   CalendarExtra: Table for storing additional availability setting for mentors like blocking date etc >> (5)

    *   block-Date(s) >> list

    *   block-dates

    *   calendarSettingList >> list to calendarSettingList table row

    *   meetingDailySessions

    *   meetingDuration

    *   meetingVenue/Link


*   VB-Vision Boards: Table to store mentees vision towards grad school application. The aim of the feature is to set mentees up for success by letting them set up a vision to meet for themself and we ensure we help them achieve every vision completion milestone with badges(actinga s a gamificatio mechanism). (10)(Discussion to deprecate or maybe we have an opportunity to build it beeter in the new app)

    *   Country Selection - listOfCountry

    *   Datecompleted

    *   datePaused

    *   dateResumed

    *   dateTargetedCompletion

    *   Document prep - documentType >> List of texts

    *   goalCompletedStatus >> bool

    *   goalName

    *   goalStatus

    *   Interview prep - interviewType >> list of texts

    *   numOfMonth

    *   numOfSessions

    *   pauseReason

    *   Program Selection - programType

    *   Program Selection - targetFieldOfStudy 

    *   Scholarship - fundingType >> list of texts

    *   School Selection - Dream School

    *   School Selection - numOfSchools

    *   sessionCompletedCount

    *   sessionMinutesCompleted

    *   Test prep - targetScore

    *   Test prep - testType

    *   totalNumberOfSessionToComplete

    *   visionBoardCardShareImg

    *   visionBoardCertShareImg

    *   visionStatement


*   messageStarters: Table for messaging initiation. >> (13)

    *   lastMessageContent

    *   messageRequestAccept

    *   receiveBy

    *   sendBy


*   messageThreads: Table for storing messaging thread >> (44)

    *   dateLastEdited

    *   messageContent

    *   messageStarter

    *   receivedBy

    *   seenRead

    *   sendBy

    *   sentAt

    *   softDelete


Planned feature data build for service type agnostic creation

*   Dedicated\_session\_types

    *   Application\_stage

    *   Fk\_mentor

    *   Is\_active

    *   Session\_category

    *   Session\_description

    *   Session\_name


*   Intake\_answers

    *   Answered\_at

    *   Fk\_intake\_submission\_id

    *   Fk\_mentee

    *   Fk\_session\_booking\_id

    *   Fk\_session\_type\_question\_id

    *   Response\_answer\_text

    *   Response\_file\_url


*   Session\_type\_booking\_configs

    *   Fk\_dedicated\_session\_type\_id

    *   Min\_booking\_notice\_minutes

    *   Session\_duration\_minutes


*   Session\_type\_questions

    *   Display\_order

    *   Fk-created\_by

    *   Fk\_dedicated\_session\_type\_id

    *   Order

    *   Question\_multi-choice

    *   Question\_required

    *   Question\_text

    *   Question\_type >> option set(free text, file upload, multi choice)
