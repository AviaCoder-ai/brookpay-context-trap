"""Domain level exceptions."""


class BrookPayError(Exception):
    """Base class for all domain errors."""


class AccountNotFound(BrookPayError):
    def __init__(self, user_id: str):
        super().__init__(f"account not found for user '{user_id}'")
        self.user_id = user_id


class UnsupportedCurrency(BrookPayError):
    def __init__(self, currency: str):
        super().__init__(f"unsupported currency '{currency}'")
        self.currency = currency


class InvalidOperation(BrookPayError):
    """Raised when a state transition or mutation is not allowed."""


class ServiceNotRegistered(BrookPayError):
    def __init__(self, name: str):
        super().__init__(f"no service registered under '{name}'")
        self.name = name
