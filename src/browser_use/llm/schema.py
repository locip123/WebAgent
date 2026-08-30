"""
Utilities for creating optimized Pydantic schemas for LLM usage.
"""

from copy import deepcopy
from typing import Any, Mapping

from pydantic import BaseModel


class SchemaOptimizer:
	@staticmethod
	def _non_nullable_schema(schema: dict[str, Any]) -> dict[str, Any]:
		"""Return the non-null member of a nullable strict-output property."""

		result = deepcopy(schema)
		options = result.get('anyOf')
		if not isinstance(options, list):
			result.pop('default', None)
			return result
		non_null_options = [option for option in options if option != {'type': 'null'}]
		if len(non_null_options) != 1:
			raise ValueError('required action parameter must have exactly one non-null schema variant')
		return deepcopy(non_null_options[0])

	@staticmethod
	def _fixed_value_schema(value: Any) -> dict[str, Any]:
		"""Return the strict JSON-schema shape for one action-branch constant."""

		if isinstance(value, bool):
			value_type = 'boolean'
		elif isinstance(value, str):
			value_type = 'string'
		elif isinstance(value, int):
			value_type = 'integer'
		elif isinstance(value, float):
			value_type = 'number'
		elif value is None:
			value_type = 'null'
		else:
			raise ValueError('action-branch fixed values must be JSON scalar values')
		return {'enum': [value], 'type': value_type}

	@staticmethod
	def _set_nested_array_min_items(
		properties: dict[str, Any],
		field_path: str,
		min_items: int,
	) -> None:
		"""Apply one array cardinality rule to a dotted decision-field path."""

		if not isinstance(min_items, int) or min_items < 0:
			raise ValueError('action-branch array minimum must be a non-negative integer')
		parts = tuple(part for part in field_path.split('.') if part)
		if not parts:
			raise ValueError('action-branch array minimum needs a non-empty field path')

		current: Any = properties
		for index, part in enumerate(parts):
			if index:
				if not isinstance(current, dict):
					raise ValueError(f'action-branch field path is not an object: {field_path!r}')
				current = current.get('properties')
				if not isinstance(current, dict):
					raise ValueError(f'action-branch field path is not an object: {field_path!r}')
			if not isinstance(current, dict) or part not in current:
				raise ValueError(f'action-branch field path does not exist: {field_path!r}')
			current = current[part]

		if not isinstance(current, dict) or current.get('type') != 'array':
			raise ValueError(f'action-branch field is not an array: {field_path!r}')
		if min_items:
			current['minItems'] = min_items
		else:
			current.pop('minItems', None)

	@staticmethod
	def _add_action_parameter_branches(
		schema: dict[str, Any],
		contracts: Mapping[str, Any],
		*,
		action_branch_variants: Mapping[str, tuple[Mapping[str, Any], ...]] | None = None,
		action_field_enums: Mapping[str, Mapping[str, tuple[Any, ...]]] | None = None,
	) -> dict[str, Any]:
		"""Constrain a flat action schema without changing its wire shape.

		OpenAI-compatible strict schemas require every root property to be present,
		which is a poor fit for a flat ``{action, ...parameters}`` object: a model
		can satisfy the transport schema while filling ``url`` on a ``click``
		decision.  Keep every property at the root for compatibility, but add an
		``anyOf`` branch for each action.  Each branch pins ``action`` to one value
		and requires every unrelated action parameter to be JSON ``null``.

		The caller attaches the resulting branches below a provider envelope rather
		than placing ``anyOf`` at the provider root. This preserves compatibility
		with strict Structured Outputs, whose root schema must remain an object.
		"""

		if schema.get('type') != 'object':
			raise ValueError('action-branch schemas require an object root')
		properties = schema.get('properties')
		if not isinstance(properties, dict) or 'action' not in properties:
			raise ValueError('action-branch schemas require an action property')
		if action_field_enums is not None and not isinstance(action_field_enums, Mapping):
			raise ValueError('action field enums must be a mapping')
		properties['action'] = {'enum': list(contracts), 'type': 'string'}

		parameter_names = frozenset(
			field_name
			for contract in contracts.values()
			for field_name in getattr(contract, 'required', frozenset())
			| getattr(contract, 'optional', frozenset())
		)
		all_property_names = list(properties)
		branches: list[dict[str, Any]] = []
		for action, contract in contracts.items():
			action_enums = action_field_enums.get(action, {}) if action_field_enums is not None else {}
			if not isinstance(action_enums, Mapping):
				raise ValueError(f'action field enums for {action!r} must be a mapping')
			variants = action_branch_variants.get(action) if action_branch_variants is not None else None
			if not variants:
				variants = ({},)
			required = frozenset(getattr(contract, 'required', frozenset()))
			allowed = frozenset(
				required
				| getattr(contract, 'optional', frozenset())
			)
			unknown_enum_fields = set(action_enums) - set(properties)
			if unknown_enum_fields:
				raise ValueError(f'action field enums do not exist: {", ".join(sorted(unknown_enum_fields))}')
			unsupported_enum_fields = set(action_enums) - set(allowed)
			if unsupported_enum_fields:
				raise ValueError(f'action field enums are not allowed for {action!r}: {", ".join(sorted(unsupported_enum_fields))}')
			for variant in variants:
				fixed_values = variant.get('fixed_values', {})
				non_nullable_fields = variant.get('non_nullable_fields', frozenset())
				array_min_items = variant.get('array_min_items', {})
				if not isinstance(fixed_values, Mapping):
					raise ValueError('action-branch fixed_values must be a mapping')
				if not isinstance(non_nullable_fields, (frozenset, set, tuple, list)):
					raise ValueError('action-branch non_nullable_fields must be a collection')
				if not isinstance(array_min_items, Mapping):
					raise ValueError('action-branch array_min_items must be a mapping')
				unknown_fields = (set(fixed_values) | set(non_nullable_fields)) - set(properties)
				if unknown_fields:
					raise ValueError(f'action-branch fields do not exist: {", ".join(sorted(unknown_fields))}')

				branch_properties: dict[str, Any] = {}
				for field_name, field_schema in properties.items():
					if field_name == 'action':
						branch_properties[field_name] = {'enum': [action], 'type': 'string'}
					elif field_name in action_enums:
						values = action_enums[field_name]
						if not isinstance(values, tuple) or not values or any(not isinstance(value, str) for value in values):
							raise ValueError(f'action field enum {action}.{field_name} must be a non-empty tuple of strings')
						branch_properties[field_name] = {'enum': list(values), 'type': 'string'}
					elif field_name in fixed_values:
						branch_properties[field_name] = SchemaOptimizer._fixed_value_schema(fixed_values[field_name])
					elif field_name in non_nullable_fields or field_name in required:
						branch_properties[field_name] = SchemaOptimizer._non_nullable_schema(field_schema)
					elif field_name in parameter_names and field_name not in allowed:
						branch_properties[field_name] = {'type': 'null'}
					else:
						branch_properties[field_name] = deepcopy(field_schema)

				for field_path, min_items in array_min_items.items():
					if not isinstance(field_path, str):
						raise ValueError('action-branch array field path must be a string')
					SchemaOptimizer._set_nested_array_min_items(branch_properties, field_path, min_items)
				branches.append(
					{
						'type': 'object',
						'properties': branch_properties,
						'required': all_property_names,
						'additionalProperties': False,
					}
				)

		result = deepcopy(schema)
		result['anyOf'] = branches
		return result

	@staticmethod
	def create_optimized_json_schema(
		model: type[BaseModel],
		*,
		remove_min_items: bool = False,
		remove_defaults: bool = False,
	) -> dict[str, Any]:
		"""
		Create the most optimized schema by flattening all $ref/$defs while preserving
		FULL descriptions and ALL action definitions. Also ensures OpenAI strict mode compatibility.

		Args:
			model: The Pydantic model to optimize
			remove_min_items: If True, remove minItems from the schema
			remove_defaults: If True, remove default values from the schema

		Returns:
			Optimized schema with all $refs resolved and strict mode compatibility
		"""
		# Generate original schema
		original_schema = model.model_json_schema()

		# Extract $defs for reference resolution, then flatten everything
		defs_lookup = original_schema.get('$defs', {})

		# Create optimized schema with flattening
		# Pass flags to optimize_schema via closure
		def optimize_schema(obj: Any, defs_lookup: dict[str, Any] | None = None, *, in_properties: bool = False) -> Any:
			"""Apply all optimization techniques including flattening all $ref/$defs"""
			if isinstance(obj, dict):
				optimized: dict[str, Any] = {}
				flattened_ref: dict[str, Any] | None = None

				# Skip unnecessary fields AND $defs (we'll inline everything)
				skip_fields = ['additionalProperties', '$defs']

				for key, value in obj.items():
					if key in skip_fields:
						continue

					# Skip metadata "title" unless we're iterating inside an actual `properties` map
					if key == 'title' and not in_properties:
						continue

					# Preserve FULL descriptions without truncation, skip empty ones
					elif key == 'description':
						if value:  # Only include non-empty descriptions
							optimized[key] = value

					# Handle type field - must recursively process in case value contains $ref
					elif key == 'type':
						optimized[key] = value if not isinstance(value, (dict, list)) else optimize_schema(value, defs_lookup)

					# FLATTEN: Resolve $ref by inlining the actual definition
					elif key == '$ref' and defs_lookup:
						ref_path = value.split('/')[-1]  # Get the definition name from "#/$defs/SomeName"
						if ref_path in defs_lookup:
							# Get the referenced definition and flatten it
							referenced_def = defs_lookup[ref_path]
							flattened_ref = optimize_schema(referenced_def, defs_lookup)

					# Skip minItems/min_items and default if requested (check BEFORE processing)
					elif key in ('minItems', 'min_items') and remove_min_items:
						continue  # Skip minItems/min_items
					elif key == 'default' and remove_defaults:
						continue  # Skip default values

					# Keep all anyOf structures (action unions) and resolve any $refs within
					elif key == 'anyOf' and isinstance(value, list):
						optimized[key] = [optimize_schema(item, defs_lookup) for item in value]

					# Recursively optimize nested structures
					elif key in ['properties', 'items']:
						optimized[key] = optimize_schema(
							value,
							defs_lookup,
							in_properties=(key == 'properties'),
						)

					# Keep essential validation fields
					elif key in [
						'required',
						'minimum',
						'maximum',
						'minItems',
						'min_items',
						'maxItems',
						'pattern',
						'default',
					]:
						optimized[key] = value if not isinstance(value, (dict, list)) else optimize_schema(value, defs_lookup)

					# Recursively process all other fields
					else:
						optimized[key] = optimize_schema(value, defs_lookup) if isinstance(value, (dict, list)) else value

				# If we have a flattened reference, merge it with the optimized properties
				if flattened_ref is not None and isinstance(flattened_ref, dict):
					# Start with the flattened reference as the base
					result = flattened_ref.copy()

					# Merge in any sibling properties that were processed
					for key, value in optimized.items():
						# Preserve descriptions from the original object if they exist
						if key == 'description' and 'description' not in result:
							result[key] = value
						elif key != 'description':  # Don't overwrite description from flattened ref
							result[key] = value

					return result
				else:
					# No $ref, just return the optimized object
					# CRITICAL: Add additionalProperties: false to ALL objects for OpenAI strict mode
					if optimized.get('type') == 'object':
						optimized['additionalProperties'] = False

					return optimized

			elif isinstance(obj, list):
				return [optimize_schema(item, defs_lookup, in_properties=in_properties) for item in obj]
			return obj

		optimized_result = optimize_schema(original_schema, defs_lookup)

		# Ensure we have a dictionary (should always be the case for schema root)
		if not isinstance(optimized_result, dict):
			raise ValueError('Optimized schema result is not a dictionary')

		optimized_schema: dict[str, Any] = optimized_result

		# A model may advertise flat action contracts through a named nested
		# property. Responses rejects root-level combinators, so keep the output
		# root a plain object and place the action branches below the envelope.
		# This is intentionally opt-in so unrelated structured outputs keep the
		# historical schema behavior.
		action_contracts = getattr(model, '__structured_action_parameter_contracts__', None)
		if action_contracts:
			action_field = getattr(model, '__structured_action_parameter_field__', None)
			if not isinstance(action_field, str) or not action_field:
				raise ValueError('action-branch schemas require a non-empty action parameter field')
			properties = optimized_schema.get('properties')
			if not isinstance(properties, dict) or action_field not in properties:
				raise ValueError(f'action-branch schema has no {action_field!r} envelope property')
			field_schema = properties[action_field]
			if not isinstance(field_schema, dict):
				raise ValueError(f'action-branch envelope property {action_field!r} must be an object schema')
			action_branch_variants = getattr(model, '__structured_action_branch_variants__', None)
			if action_branch_variants is not None and not isinstance(action_branch_variants, Mapping):
				raise ValueError('action-branch variants must be a mapping')
			action_field_enums = getattr(model, '__structured_action_field_enums__', None)
			if action_field_enums is not None and not isinstance(action_field_enums, Mapping):
				raise ValueError('action field enums must be a mapping')
			properties[action_field] = SchemaOptimizer._add_action_parameter_branches(
				field_schema,
				action_contracts,
				action_branch_variants=action_branch_variants,
				action_field_enums=action_field_enums,
			)

		# Additional pass to ensure ALL objects have additionalProperties: false
		def ensure_additional_properties_false(obj: Any) -> None:
			"""Ensure all objects have additionalProperties: false"""
			if isinstance(obj, dict):
				# If it's an object type, ensure additionalProperties is false
				if obj.get('type') == 'object':
					obj['additionalProperties'] = False

				# Recursively apply to all values
				for value in obj.values():
					if isinstance(value, (dict, list)):
						ensure_additional_properties_false(value)
			elif isinstance(obj, list):
				for item in obj:
					if isinstance(item, (dict, list)):
						ensure_additional_properties_false(item)

		ensure_additional_properties_false(optimized_schema)
		SchemaOptimizer._make_strict_compatible(optimized_schema)

		# Final pass to remove minItems/min_items and default values if requested
		if remove_min_items or remove_defaults:

			def remove_forbidden_fields(obj: Any) -> None:
				"""Recursively remove minItems/min_items and default values"""
				if isinstance(obj, dict):
					# Remove forbidden keys
					if remove_min_items:
						obj.pop('minItems', None)
						obj.pop('min_items', None)
					if remove_defaults:
						obj.pop('default', None)
					# Recursively process all values
					for value in obj.values():
						if isinstance(value, (dict, list)):
							remove_forbidden_fields(value)
				elif isinstance(obj, list):
					for item in obj:
						if isinstance(item, (dict, list)):
							remove_forbidden_fields(item)

			remove_forbidden_fields(optimized_schema)

		return optimized_schema

	@staticmethod
	def _make_strict_compatible(schema: dict[str, Any] | list[Any]) -> None:
		"""Ensure all properties are required for OpenAI strict mode"""
		if isinstance(schema, dict):
			# First recursively apply to nested objects
			for key, value in schema.items():
				if isinstance(value, (dict, list)) and key != 'required':
					SchemaOptimizer._make_strict_compatible(value)

			# Then update required for this level
			if 'properties' in schema and 'type' in schema and schema['type'] == 'object':
				# Add all properties to required array
				all_props = list(schema['properties'].keys())
				schema['required'] = all_props  # Set all properties as required

		elif isinstance(schema, list):
			for item in schema:
				SchemaOptimizer._make_strict_compatible(item)

	@staticmethod
	def create_gemini_optimized_schema(model: type[BaseModel]) -> dict[str, Any]:
		"""
		Create Gemini-optimized schema, preserving explicit `required` arrays so Gemini
		respects mandatory fields defined by the caller.

		Args:
			model: The Pydantic model to optimize

		Returns:
			Optimized schema suitable for Gemini structured output
		"""
		return SchemaOptimizer.create_optimized_json_schema(model)
