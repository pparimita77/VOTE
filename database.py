"""
database.py
-----------
All MongoDB access for the Voting Giveaway Bot lives here.
Uses Motor (async MongoDB driver) so it plays nicely with python-telegram-bot's
async handlers.

Collections
-----------
users         -> every user who has ever pressed /start, verified status
giveaways     -> one document per giveaway
participants  -> one document per (giveaway, user) that joined a giveaway
votes         -> one document per (giveaway, voter) - enforces "one vote per person"
"""

import logging
from datetime import datetime, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, mongo_uri: str, db_name: str):
        self.client = AsyncIOMotorClient(mongo_uri)
        self.db = self.client[db_name]
        self.users = self.db["users"]
        self.giveaways = self.db["giveaways"]
        self.participants = self.db["participants"]
        self.votes = self.db["votes"]

    async def init_indexes(self):
        await self.users.create_index("user_id", unique=True)
        await self.giveaways.create_index("status")
        await self.participants.create_index(
            [("giveaway_id", 1), ("user_id", 1)], unique=True
        )
        await self.votes.create_index(
            [("giveaway_id", 1), ("voter_id", 1)], unique=True
        )
        await self.votes.create_index([("giveaway_id", 1), ("candidate_id", 1)])
        logger.info("MongoDB indexes ready.")

    # =========================================================
    #  USERS
    # =========================================================
    async def upsert_user(self, user_id: int, username: str, first_name: str):
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {"username": username, "first_name": first_name},
                "$setOnInsert": {
                    "verified": False,
                    "created_at": datetime.utcnow(),
                },
            },
            upsert=True,
        )

    async def set_verified(self, user_id: int):
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"verified": True, "verified_at": datetime.utcnow()}},
        )

    async def is_verified(self, user_id: int) -> bool:
        u = await self.users.find_one({"user_id": user_id})
        return bool(u and u.get("verified"))

    async def get_user(self, user_id: int):
        return await self.users.find_one({"user_id": user_id})

    # =========================================================
    #  GIVEAWAYS
    # =========================================================
    async def create_giveaway(
        self, title: str, total_winners: int, duration_minutes: int, created_by: int
    ) -> str:
        now = datetime.utcnow()
        doc = {
            "title": title,
            "total_winners": total_winners,
            "duration_minutes": duration_minutes,
            "created_by": created_by,
            "created_at": now,
            "ends_at": now + timedelta(minutes=duration_minutes),
            "status": "active",  # active -> ended
            "channel_chat_id": None,
            "channel_message_id": None,
        }
        res = await self.giveaways.insert_one(doc)
        return str(res.inserted_id)

    async def set_channel_message(self, giveaway_id: str, chat_id: int, message_id: int):
        await self.giveaways.update_one(
            {"_id": self._oid(giveaway_id)},
            {"$set": {"channel_chat_id": chat_id, "channel_message_id": message_id}},
        )

    async def get_giveaway(self, giveaway_id: str):
        return await self.giveaways.find_one({"_id": self._oid(giveaway_id)})

    async def get_active_giveaway(self):
        return await self.giveaways.find_one(
            {"status": "active"}, sort=[("created_at", -1)]
        )

    async def get_latest_giveaway(self):
        return await self.giveaways.find_one({}, sort=[("created_at", -1)])

    async def get_all_active_giveaways(self):
        cursor = self.giveaways.find({"status": "active"})
        return await cursor.to_list(length=None)

    async def end_giveaway(self, giveaway_id: str):
        await self.giveaways.update_one(
            {"_id": self._oid(giveaway_id)}, {"$set": {"status": "ended"}}
        )

    # =========================================================
    #  PARTICIPANTS
    # =========================================================
    async def add_participant(
        self, giveaway_id: str, user_id: int, username: str, first_name: str
    ) -> bool:
        """Returns False if the user already joined this giveaway."""
        try:
            await self.participants.insert_one(
                {
                    "giveaway_id": giveaway_id,
                    "user_id": user_id,
                    "username": username,
                    "first_name": first_name,
                    "votes": 0,
                    "disqualified": False,
                    "disqualify_reason": None,
                    "message_id": None,
                    "joined_at": datetime.utcnow(),
                }
            )
            return True
        except DuplicateKeyError:
            return False

    async def get_participant(self, giveaway_id: str, user_id: int):
        return await self.participants.find_one(
            {"giveaway_id": giveaway_id, "user_id": user_id}
        )

    async def set_participant_message(self, giveaway_id: str, user_id: int, message_id: int):
        await self.participants.update_one(
            {"giveaway_id": giveaway_id, "user_id": user_id},
            {"$set": {"message_id": message_id}},
        )

    async def get_participants(self, giveaway_id: str):
        cursor = self.participants.find({"giveaway_id": giveaway_id})
        return await cursor.to_list(length=None)

    async def top_participants(self, giveaway_id: str, limit: int = 10):
        cursor = (
            self.participants.find({"giveaway_id": giveaway_id, "disqualified": False})
            .sort("votes", -1)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    async def increment_vote(self, giveaway_id: str, candidate_id: int):
        await self.participants.update_one(
            {"giveaway_id": giveaway_id, "user_id": candidate_id},
            {"$inc": {"votes": 1}},
        )

    async def add_votes(self, giveaway_id: str, user_id: int, amount: int) -> bool:
        """Manually adjust a participant's vote count by `amount` (can be negative).
        Used by the owner-only /addvote command. Returns False if the participant
        isn't found in this giveaway. Vote count is floored at 0."""
        participant = await self.participants.find_one(
            {"giveaway_id": giveaway_id, "user_id": user_id}
        )
        if not participant:
            return False

        new_votes = max(0, participant.get("votes", 0) + amount)
        result = await self.participants.update_one(
            {"giveaway_id": giveaway_id, "user_id": user_id},
            {"$set": {"votes": new_votes}},
        )
        return result.matched_count > 0

    async def disqualify_participant(self, giveaway_id: str, user_id: int, reason: str):
        await self.participants.update_one(
            {"giveaway_id": giveaway_id, "user_id": user_id},
            {"$set": {"disqualified": True, "disqualify_reason": reason}},
        )

    # =========================================================
    #  VOTES
    # =========================================================
    async def has_voted(self, giveaway_id: str, voter_id: int):
        return await self.votes.find_one(
            {"giveaway_id": giveaway_id, "voter_id": voter_id}
        )

    async def add_vote(self, giveaway_id: str, voter_id: int, candidate_id: int) -> bool:
        """Returns False if this voter already voted in this giveaway (one vote per person)."""
        try:
            await self.votes.insert_one(
                {
                    "giveaway_id": giveaway_id,
                    "voter_id": voter_id,
                    "candidate_id": candidate_id,
                    "voted_at": datetime.utcnow(),
                }
            )
            return True
        except DuplicateKeyError:
            return False

    async def recent_votes_count(self, giveaway_id: str, candidate_id: int, window_seconds: int) -> int:
        since = datetime.utcnow() - timedelta(seconds=window_seconds)
        return await self.votes.count_documents(
            {
                "giveaway_id": giveaway_id,
                "candidate_id": candidate_id,
                "voted_at": {"$gte": since},
            }
        )

    # =========================================================
    #  HELPERS
    # =========================================================
    @staticmethod
    def _oid(giveaway_id: str) -> ObjectId:
        try:
            return ObjectId(giveaway_id)
        except InvalidId:
            raise ValueError("Invalid giveaway id")