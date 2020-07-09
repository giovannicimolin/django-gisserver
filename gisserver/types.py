"""Internal XSD type definitions.

These types are the internal definition on which all output is generated.
It's constructed from the model metadata by the `FeatureType` / `FeatureField`
classes. Custom field types could also generate these field types.
"""
import operator
import re
from dataclasses import dataclass
from enum import Enum

from django.core.exceptions import (
    ObjectDoesNotExist,
    ValidationError,
)
from django.db.models import Q
from django.db.models.fields.related import RelatedField
from typing import List, Optional, Tuple, Type

from django.contrib.gis.db.models import GeometryField
from django.db import models
from django.utils.functional import cached_property

from gisserver.geometries import CRS, WGS84  # noqa, for backwards compatibility

__all__ = [
    "XsdAnyType",
    "XsdTypes",
    "XsdComplexType",
    "strip_namespace_prefix",
]


RE_XPATH_ATTR = re.compile(r"\[[^\]]+\]$")
RE_NON_NAME = re.compile(r"[^a-zA-Z0-9_/]")


class XsdAnyType:
    """Base class for all types used in the XML definition"""

    prefix = None
    is_complex_type = False

    def __str__(self):
        raise NotImplementedError()

    def with_prefix(self, prefix="xs"):
        raise NotImplementedError()


class XsdTypes(XsdAnyType, Enum):
    """Brief enumeration of basic XSD-types.

    The default namespace is the "xs:" (XMLSchema).
    Based on https://www.w3.org/TR/xmlschema-2/#built-in-datatypes
    """

    anyType = "anyType"  # Needs to be anyType, as "xsd:any" is an element, not a type.
    string = "string"
    boolean = "boolean"
    decimal = "decimal"  # the base type for all numbers too.
    integer = "integer"
    float = "float"
    double = "double"
    time = "time"
    date = "date"
    dateTime = "dateTime"
    anyURI = "anyURI"

    # Less common, but useful nonetheless:
    duration = "duration"
    nonNegativeInteger = "nonNegativeInteger"
    gYear = "gYear"
    hexBinary = "hexBinary"
    base64Binary = "base64Binary"
    token = "token"
    language = "language"

    # Types that contain a GML value as member:
    gmlGeometryPropertyType = "gml:GeometryPropertyType"
    gmlPointPropertyType = "gml:PointPropertyType"
    gmlCurvePropertyType = "gml:CurvePropertyType"  # curve is base for LineString
    gmlSurfacePropertyType = "gml:SurfacePropertyType"  # GML2 had PolygonPropertyType
    gmlMultiSurfacePropertyType = "gml:MultiSurfacePropertyType"
    gmlMultiPointPropertyType = "gml:MultiPointPropertyType"
    gmlMultiCurvePropertyType = "gml:MultiCurvePropertyType"
    gmlMultiGeometryPropertyType = "gml:MultiGeometryPropertyType"

    #: A direct geometry value
    gmlAbstractGeometryType = "gml:AbstractGeometryType"

    #: A feature that has an gml:name and gml:boundedBy as posible child element.
    gmlAbstractFeatureType = "gml:AbstractFeatureType"

    def __str__(self):
        return self.value

    @property
    def prefix(self) -> Optional[str]:
        colon = self.value.find(":")
        return self.value[:colon] if colon else None

    def with_prefix(self, prefix="xs"):
        if ":" in self.value:
            return self.value
        else:
            return f"{prefix}:{self.value}"


@dataclass(frozen=True)
class XsdElement:
    """Declare an XSD element.

    This holds the definition for a single property in the WFS server.
    It's used in ``DescribeFeatureType`` to output the field meta data,
    and used in ``GetFeature`` to access the actual value from the object.
    Overriding :meth:`get_value` allows to override this logic.

    The :attr:`name` may differ from the underlying :attr:`model_attribute`,
    so the WFS server can use other field names then the underlying model.

    A dotted-path notation can be used for :attr:`model_attribute` to access
    a related field. For the WFS client, the data appears to be flattened.
    """

    name: str
    type: XsdAnyType  # Both XsdComplexType and XsdType are allowed
    nillable: Optional[bool] = None
    min_occurs: Optional[int] = None
    max_occurs: Optional[int] = None
    source: Optional[models.Field] = None

    #: Which field to read from the model to get the value
    #: This supports dot notation to access related attributes.
    model_attribute: Optional[str] = None

    def __post_init__(self):
        if self.model_attribute is None:
            object.__setattr__(self, "model_attribute", self.name)

        # Using operator.attrgetter() instead of getattr() gives built-in
        # support to traversing model attributes with dots.
        object.__setattr__(
            self, "_attrgetter", operator.attrgetter(self.model_attribute)
        )

    @cached_property
    def is_gml(self) -> bool:
        return isinstance(self.source, GeometryField) or self.type.prefix == "gml"

    @cached_property
    def is_flattened(self) -> bool:
        """Whether the field is a lookup to a relation."""
        return "." in self.model_attribute

    @cached_property
    def as_xml(self):
        attributes = [f'name="{self.name}" type="{self.type}"']
        if self.min_occurs is not None:
            attributes.append(f'minOccurs="{self.min_occurs}"')
        if self.max_occurs is not None:
            attributes.append(f'maxOccurs="{self.max_occurs}"')
        if self.nillable:
            str_bool = "true" if self.nillable else "false"
            attributes.append(f'nillable="{str_bool}"')

        return "<element {} />".format(" ".join(attributes))

    def __str__(self):
        return self.as_xml

    @cached_property
    def orm_path(self) -> str:
        """The ORM field lookup to perform."""
        return self.model_attribute.replace(".", "__")

    @cached_property
    def orm_relation(self) -> Tuple[str, str]:
        """The ORM field and parent relation"""
        try:
            path, field = self.model_attribute.rsplit(".", 1)
        except ValueError:
            return None, self.model_attribute
        else:
            return path.replace(".", "__"), field

    def get_value(self, instance: models.Model):
        """Provide the value for the data"""
        # For foreign keys, it's not possible to use the model value,
        # as that would conflict with the field type in the XSD schema.
        try:
            return self._attrgetter(instance)
        except (AttributeError, ObjectDoesNotExist):
            # E.g. Django foreign keys that point to a non-existing member.
            return None

    def validate_comparison(self, raw_value: str, lookup, tag=None):
        """Validate whether the input value can be used in a comparison.
        This avoids comparing a database DATETIME object to an integer.

        The raw string value can be passed here. Auto-cased values could
        raise an TypeError due to being unsupported by the validation.
        """
        if self.source is not None:
            # Not calling self.source.validate() as that checks for allowed choices,
            # which shouldn't be checked against for a filter query.
            try:
                self.source.get_prep_value(raw_value)
            except ValidationError as e:
                raise ValidationError(
                    f"Invalid data for the '{self.name}' property: {e.messages[0]}",
                    code=e.code,
                ) from e
            except (ValueError, TypeError) as e:
                raise ValidationError(
                    f"Invalid data for the '{self.name}' property: {e}"
                ) from e

            # Check whether the Django model field supports the lookup
            # This prevents calling LIKE on a datetime or float field.
            # For foreign keys, this depends on the target field type.
            if self.source.get_lookup(lookup) is None or (
                isinstance(self.source, RelatedField)
                and self.source.target_field.get_lookup(lookup) is None
            ):
                raise ValidationError(
                    f"Operator '{tag}' is not supported for the '{self.name}' property."
                )


class _XsdElement_WithComplexType(XsdElement):
    """This only exists as "protocol" for the type annotations"""

    type: "XsdComplexType"


@dataclass(frozen=True)
class XsdComplexType(XsdAnyType):
    """Define an <xsd:complexType> that represents a whole class definition.

    Objects of this type are typically generated by the ``FeatureType`` and
    ``ComplexFeatureField`` classes, using the Django model data.

    This element defines the field names and field elements used in the
    WFS server. This is the basis for ``DescribeFeatureType`` and locating
    the queried properties in FES filter queries.

    By default, The type is declared as subclass of <gml:AbstractFeatureType>,
    which allows child elements like <gml:name> and <gml:boundedBy>.
    """

    name: str
    elements: List[XsdElement]
    base: XsdTypes = XsdTypes.gmlAbstractFeatureType
    source: Optional[Type[models.Model]] = None

    def __str__(self):
        return f"{self.prefix}:{self.name}"

    @property
    def is_complex_type(self):
        return True

    @property
    def prefix(self):
        # mimic API of XsdTypes
        return "app"

    def with_prefix(self, prefix="xs"):
        # mimic API of XsdTypes
        return str(self)

    @cached_property
    def gml_elements(self) -> List[XsdElement]:
        """Shortcut to get all geometry elements"""
        return [e for e in self.elements if e.is_gml]

    @cached_property
    def complex_elements(self) -> List[_XsdElement_WithComplexType]:
        """Shortcut to get all elements with a complex type"""
        return [e for e in self.elements if e.type.is_complex_type]

    @cached_property
    def flattened_elements(self) -> List[XsdElement]:
        """Shortcut to get all elements with a flattened model attribite"""
        return [e for e in self.elements if e.is_flattened]

    def resolve_element_path(self, xpath: str) -> Optional[List[XsdElement]]:
        """Resolve an xpath reference to the actual node.
        This returns the list of all levels if a match was found.
        """
        try:
            pos = xpath.rindex("/")
            node_name = xpath[:pos]
        except ValueError:
            node_name = xpath
            pos = 0

        # Strip current app namespace. Note this should actually compare the
        # xmlns URI's, but this will suffice for now. The ElementTree parser
        # doesn't provide access to 'xmlns' definitions on the element (or it's
        # parents), so a tag like this is essentially not parsable for us:
        # <ValueReference xmlns:tns="http://example.org/gisserver">tns:fieldname</ValueReference>
        node_name = strip_namespace_prefix(node_name)

        # Strip any [@attr=..] conditions
        node_name = RE_XPATH_ATTR.sub("", node_name)

        for element in self.elements:
            if element.name == node_name:
                if pos:
                    if not element.type.is_complex_type:
                        return None
                    else:
                        child_path = element.type.resolve_element_path(xpath[pos + 1 :])
                        if child_path is None:
                            return None
                        else:
                            return [element] + child_path
                else:
                    return [element]

        return None


def strip_namespace_prefix(value: str):
    """Remove the namespace prefix from an element."""
    try:
        ns_pos = value.index(":")
        return value[ns_pos + 1 :]
    except ValueError:
        return value


class XPathMatch:
    """Wrapper class to provide XPath results."""

    #: The matched element, with all it's parents.
    elements: List[XsdElement]

    #: The source XPath query
    query: str

    def __init__(self, elements: List[XsdElement], query: str):
        self.elements = elements
        self.query = query

    def __iter__(self):
        return iter(self.elements)

    def __getitem__(self, item) -> XsdElement:
        return self.elements[item]

    def __repr__(self):
        return f"XPathMatch(elements={self.elements!r}, query={self.query!r})"

    @property
    def child(self) -> XsdElement:
        """Return only the final element"""
        return self.elements[-1]

    @property
    def orm_path(self) -> str:
        """Tell which ORM path should be targetted."""
        return "__".join(xsd_element.orm_path for xsd_element in self.elements)

    @property
    def orm_filters(self) -> Optional[Q]:
        """Tell which additional filters are needed (due to [@attr=..] syntax)."""
        if RE_NON_NAME.match(self.query):
            # If there is an element[@attr=...]/field tag,
            # the build_...() logic should return a Q() object.
            raise NotImplementedError(
                f"Complex XPath queries are not supported yet: {self.query}"
            )

        return None
