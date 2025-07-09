from . import TychoDB


class TychoDBSingleton:
    """
    A singleton wrapper around the TychoDB class.

    This class ensures that there is only one instance of TychoDB throughout the lifetime of the program,
    avoiding the overhead of creating multiple instances.
    """

    _instance = None

    @classmethod
    def initialize(cls, tycho_http_url: str = ""):
        """
        Initialize the TychoDB instance with the given URLs. Does nothing if an instance already exists.

        Parameters
        ----------
        tycho_http_url : str
            The URL of the Tycho HTTP server.

        """
        if cls._instance is None:
            cls._instance = TychoDB(tycho_http_url)

    @classmethod
    def get_instance(cls) -> TychoDB:
        """
        Retrieve the singleton instance of TychoDB.

        Errors if an instance does not exist.

        Returns
        -------
        TychoDB
            The singleton instance of TychoDB.
        """
        if cls._instance is None:
            raise ValueError(
                "TychoDB instance not initialized. Call initialize() first."
            )
        return cls._instance

    @classmethod
    def clear_instance(cls):
        cls._instance = None
