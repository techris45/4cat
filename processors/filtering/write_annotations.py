"""
Write annotations to a dataset
"""
from processors.filtering.base_filter import BaseFilter
from common.lib.helpers import UserInput

__author__ = "Sal Hagen"
__credits__ = ["Sal Hagen"]
__maintainer__ = "Sal Hagen"
__email__ = "4cat@oilab.eu"


class WriteAnnotations(BaseFilter):
	"""
	Write annotated data from the Explorer to a dataset.
	"""
	type = "write-annotations"  # job type ID
	category = "Filtering"  # category
	title = "Write annotations"  # title displayed in UI
	description = "Writes annotations from the Explorer to the dataset. Each input field will get a column. This creates a new dataset."  # description displayed in UI

	options = {
		"to-lowercase": {
			"type": UserInput.OPTION_TOGGLE,
			"default": False,
			"help": "Convert annotations to lowercase"
		}
	}

	@classmethod
	def is_compatible_with(cls, module=None):
		"""
		Allow processor on CSV files

		:param module: Dataset or processor to determine compatibility with
		"""
		return module.is_top_dataset()

	def filter_items(self):
		"""
		Create a generator to iterate through items that can be passed to create either a csv or ndjson. Use
		`for original_item, mapped_item in self.source_dataset.iterate_mapped_items(self)` to iterate through items
		and yield `original_item`.

		:return generator:
		"""
		# Load annotation fields and annotations
		annotations = self.dataset.get_annotations()
		annotation_fields = self.dataset.get_annotation_fields()
		
		# If there are no fields or annotations saved, we're done here
		if not annotation_fields:
			self.dataset.update_status("This dataset has no annotation fields saved.")
			self.dataset.finish(0)
			return 
		if not annotations:
			self.dataset.update_status("This dataset has no annotations saved.")
			self.dataset.finish(0)
			return

		annotation_labels = [v["label"] for v in annotation_fields.values()]

		to_lowercase = self.parameters.get("to-lowercase", False)
		annotated_posts = set(annotations.keys())
		post_count = 0
		# iterate through posts and check if they appear in the annotations
		for original_item, mapped_item in self.source_dataset.iterate_mapped_items(self):
			post_count += 1

			# Write the annotations to this row if they're present
			if mapped_item["id"] in annotated_posts:
				post_annotations = annotations[mapped_item["id"]]

				# We're adding (empty) values for every field
				for field in annotation_labels:

					if field in post_annotations:

						val = post_annotations[field]

						# We join lists (checkboxes)
						if isinstance(val, list):
							val = ", ".join(val)
						# Convert to lowercase if indicated
						if to_lowercase:
							val = val.lower()

						# TODO: writting to ndjson is not visible in map_item/frontend
						original_item[field] = val
					else:
						original_item[field] = ""

			# Write empty values if this post has not been annotated
			else:
				for field in annotation_labels:
					original_item[field] = ""

			yield original_item

			if post_count % 2500 == 0:
				self.dataset.update_status("Processed %i posts" % post_count)
				self.dataset.update_progress(post_count / self.source_dataset.num_rows)
