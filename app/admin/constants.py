
# 3 is unused
class ReportTypes:
    ALL = -1
    USER = 0
    POST = 1
    COMMENT = 2
    DM = 4

    @classmethod
    def get_choices(cls):
        return [
            (cls.ALL, "All"),
            (cls.USER, "User"),
            (cls.POST, "Post"),
            (cls.COMMENT, "Comment"),
            (cls.DM, "Direct Message"),
        ]
