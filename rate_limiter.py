from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import threading
import time
from typing import Protocol

class StoreUnavailable(Exception):
    """The log store could not answer: timeout, connection lost, node down."""

class Channel(StrEnum):
    EMAIL = "EMAIL"
    SMS = "SMS"


class Outcome(StrEnum):
    ALLOWED = "ALLOWED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class RejectionReason(StrEnum):
    RATE_LIMITED = "RATE_LIMITED"
    STORE_UNAVAILABLE = "STORE_UNAVAILABLE"


class ErrorReason(StrEnum):
    UNKNOWN_USER = "UNKNOWN_USER"


class AdmitStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    SEEN = "SEEN"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Decision:
    """The answer the caller gets. rejection_reason and retry_after are set
    only when REJECTED. error_reason is set only when ERROR. fallback is True
    when the answer came from the outage policy, not a normal check."""

    outcome: Outcome
    rejection_reason: RejectionReason | None = None
    retry_after: float | None = None
    fallback: bool = False
    error_reason: ErrorReason | None = None


@dataclass(frozen=True)
class AdmitResult:
    """The store's answer to try_admit. blocked_scopes and retry_after only mean
    something when status is REJECTED. retry_after is in seconds."""

    status: AdmitStatus
    blocked_scopes: tuple[str, ...] = ()
    retry_after: float = 0.0
    limiting_scope: str | None = None


@dataclass(frozen=True)
class ScopeLimit:
    """One level for the store to check. scope is an opaque label such as
    "user". The store builds the key from scope and id."""

    scope: str
    id: str
    limit: int


@dataclass(frozen=True)
class Limits:
    """Caps per window. window_seconds is how long a send counts."""
# Rate limiter doesnt write from config into this , we recieve it only ext 
    user_cap: int
    team_cap: int
    company_cap: int
    window_seconds: float = 600.0



@dataclass(frozen=True)
class Membership:
    team_id: str
    company_id: str


class FallbackPolicy(Protocol):
    def on_store_failure(self)->Decision:...

class FailOpen:
    def on_store_failure(self) -> Decision:
        return Decision(outcome=Outcome.ALLOWED, fallback=True)

class FailClosed:
    def on_store_failure(self) -> Decision:
        return Decision(
            outcome=Outcome.REJECTED,
            rejection_reason=RejectionReason.STORE_UNAVAILABLE,
            fallback=True,
        )
     

#placeholders for now

class OrgLookup(Protocol):
    def lookup(self,user_id:str)->Membership|None : ...


class MappingCache:
    def __init__(
            self,
            org_lookup:OrgLookup,
            ttl_seconds:float,
            clock:Callable[[],float] = time.monotonic,
    ):
        self.org_lookup = org_lookup
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._entries: dict[str,tuple[Membership,float]] = {}
    def get_mapping(self,user_id:str)->Membership|None:
        # check for the entry 
        if user_id in self._entries:
            entry = self._entries.get(user_id)
            if self.clock()-entry[1] < self.ttl_seconds:
                return entry[0]
        membership = self.org_lookup.lookup(user_id)
        if membership is not None:
            self._entries[user_id] = (membership,self.clock())
            return membership
        else:
            return None 
        # if nto in dict call orgservice lookup 

         




class LogStore:
    def __init__(
            self,
            limits: Limits,
            clock: Callable[[],float] = time.monotonic,
    ):
        self.clock = clock
        self.limits = limits
        self._lock = threading.Lock()
        self._logs:dict[str,list[tuple[float,str]]] = {}
        self._expires_at:dict[str,float] = {}


    @staticmethod
    def _key(scope:str,id:str)->str:
        return scope+":"+id

    def try_admit(self,scopes:list[ScopeLimit],request_id:str)->AdmitResult:
        with self._lock:
            # build the keys
            key_targets = [(self._key(sl.scope,sl.id),sl) for sl in scopes]
            # cleanup the stale cases based t > ts 
            # clean using strategy of rebuild after analysing not , delete while iterate 
            now = self.clock()
            cutoff = now - self.limits.window_seconds
            for key,sl in key_targets:
                self._logs[key] = [(ts,rid) for ts,rid in self._logs.get(key,[]) if ts>cutoff]
                    
            # check if user key is duplicate request : scope[0] -> user level
            user_key,_ = key_targets[0] # 
            if any(rid ==request_id for _,rid in self._logs.get(user_key,[])):
                return AdmitResult(status=AdmitStatus.SEEN)

            #check for hthe bocked scopes 
            blocked_scopes:list[str] = []
            retry_after:float = 0.0
            lim_scope: str | None = None 
            # at each level const
            for key,sl in key_targets:
                log = self._logs.get(key,[])
                if len(log)>=sl.limit:
                    blocked_scopes.append(sl.scope)
                    frees_at = log[0][0] + self.limits.window_seconds
                    if (frees_at - now) > retry_after:
                        retry_after,lim_scope = frees_at - now,sl.scope
            # if blockec scope non 0 return rejection 
            if(len(blocked_scopes)!=0):
                return AdmitResult(
                    status=AdmitStatus.REJECTED,
                    blocked_scopes=tuple(blocked_scopes),
                    retry_after=retry_after,
                    limiting_scope=lim_scope
                )
            for key,sl in key_targets:
                log = self._logs.get(key,[])
                self._expires_at[key] = now + self.limits.window_seconds
                log.append((now,request_id))

            return AdmitResult(status=AdmitStatus.ACCEPTED)


class RateLimiter:


    def __init__(
            self,
            mapping_cache: MappingCache,
            log_store: LogStore,
            limits: Limits,
            policies: dict[Channel,FallbackPolicy]
    ):
        self.mapping_cache = mapping_cache
        self.log_store=log_store
        self.limits=limits
        self.policies=policies

    def allow(self,request_id:str,user_id:str,channel:Channel)->Decision:
        # build scope list from some config 

        # get the membership
        membership: Membership | None = self.mapping_cache.get_mapping(user_id)
        if membership is None:
            return Decision(outcome=Outcome.ERROR,error_reason=ErrorReason.UNKNOWN_USER)
        scopes = [
            ScopeLimit("user",user_id,self.limits.user_cap),
            ScopeLimit("team",membership.team_id,self.limits.team_cap),
            ScopeLimit("company",membership.company_id,self.limits.company_cap),
        ]

        # build the scopes 
        try:
            result:AdmitResult = self.log_store.try_admit(scopes,request_id)
        except StoreUnavailable:
            return self.policies[channel].on_store_failure()

        if result.status == AdmitStatus.SEEN or result.status == AdmitStatus.ACCEPTED:
            return Decision(Outcome.ALLOWED,None,None)
        else:
            return Decision(Outcome.REJECTED,RejectionReason.RATE_LIMITED,result.retry_after)

        
 
    
    