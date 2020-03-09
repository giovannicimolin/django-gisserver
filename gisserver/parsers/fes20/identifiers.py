"""These classes map to the FES 2.0 specification for identifiers.
The class names are identical to those in the FES spec.
"""
import operator
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from functools import reduce
from typing import Optional, Union, List

from django.db.models import Q

from gisserver.parsers.base import FES20, BaseNode, tag_registry

NoneType = type(None)


class VersionActionTokens(Enum):
    FIRST = "FIRST"
    LAST = "LAST"
    ALL = "ALL"
    NEXT = "NEXT"
    PREVIOUS = "PREVIOUS"


class Id(BaseNode):
    """Abstract base class, as defined by FES spec."""

    xml_ns = FES20

    def build_value(self, fesquery) -> Q:
        raise NotImplementedError()


class IdList(List[Id]):
    """List of ResourceId objects"""

    def build_query(self, fesquery) -> Q:
        """Generate the ID lookup query"""
        return reduce(operator.or_, [id.build_value(fesquery) for id in self],)


@dataclass
@tag_registry.register("ResourceId")
class ResourceId(Id):
    """The <fes:ResourceId> element."""

    rid: str
    version: Union[int, datetime, VersionActionTokens, NoneType] = None
    startTime: Optional[datetime] = None
    endTime: Optional[datetime] = None

    @classmethod
    def from_xml(cls, element):
        version = element.get("version")
        startTime = element.get("startTime")
        endTime = element.get("endTime")

        if version:
            if version.isdigit():
                version = int(version)

            if "T" in version:
                try:
                    version = datetime.fromisoformat(version)
                except ValueError:
                    pass

        return cls(
            rid=element.attrib["rid"],
            version=version,
            startTime=datetime.fromisoformat(startTime) if startTime else None,
            endTime=datetime.fromisoformat(endTime) if endTime else None,
        )

    def build_value(self, fesquery) -> Q:
        """Render the SQL filter"""
        if self.startTime or self.endTime or self.version:
            raise NotImplementedError(
                "No support for <fes:ResourceId> startTime/endTime/version attributes"
            )
        return Q(pk=self.rid)
