"""Application navigation labels and stable filter links."""

from sarthi.db.application_dashboard import ApplicationSort, ApplicationView

APPLICATION_TABS = {
    ApplicationView.ALL: "All",
    ApplicationView.INPUT: "Answer or approve",
    ApplicationView.CAPTCHA: "Solve CAPTCHA",
    ApplicationView.MANUAL: "Apply yourself",
    ApplicationView.SUBMITTED: "Submitted",
    ApplicationView.VERIFY: "Verify submission",
    ApplicationView.QUEUED: "In progress",
    ApplicationView.STOPPED: "Closed or stopped",
}


def application_view(show: str) -> ApplicationView:
    """Use the all-applications view for an unknown filter."""
    try:
        return ApplicationView(show)
    except ValueError:
        return ApplicationView.ALL


APPLICATION_SORTS = {
    ApplicationSort.NEWEST: "Recently updated",
    ApplicationSort.OLDEST: "Oldest first",
    ApplicationSort.COMPANY: "Company A-Z",
    ApplicationSort.ROLE: "Role A-Z",
}


def application_sort(value: str) -> ApplicationSort:
    """Use the default sort for an unknown order."""
    try:
        return ApplicationSort(value)
    except ValueError:
        return ApplicationSort.NEWEST
