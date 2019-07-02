import shutil
import time

from pymysql import OperationalError, ProgrammingError, Error
from collections import Counter
from abc import ABC, abstractmethod

import config
from backend.lib.database_mysql import MySQLDatabase
from backend.lib.dataset import DataSet
from backend.abstract.processor import BasicProcessor
from backend.lib.helpers import posts_to_csv, get_software_version


class SphinxSearch(BasicProcessor, ABC):
	"""
	Process substring queries from the front-end

	Requests are added to the pool as "query" jobs. This class is to be
	extended by data source-specific search classes, which will define the
	abstract methods at the end of this class to tailor the search engine
	to their database layouts.
	"""

	type = "query"
	max_workers = 2

	prefix = ""
	sphinx_index = ""

	sphinx = None
	dataset = None

	# Columns to return in csv
	# Mandatory columns: ['thread_id', 'body', 'subject', 'timestamp']
	return_cols = ['thread_id', 'id', 'timestamp', 'body', 'subject', 'author', 'image_file', 'image_md5',
				   'country_code', 'country_name']

	# not available as a processor for existing datasets
	accepts = [None]

	def process(self):
		"""
		Run 4CAT search query

		Gets query details, passes them on to the object's search method, and
		writes the results to a CSV file. If that all went well, the query and
		job are marked as finished.
		"""

		# connect to Sphinx backend
		self.sphinx = MySQLDatabase(
			host="localhost",
			user=config.DB_USER,
			password=config.DB_PASSWORD,
			port=9306,
			logger=self.log
		)

		query_parameters = self.dataset.get_parameters()
		results_file = self.dataset.get_results_path()

		self.log.info("Querying: %s" % str(query_parameters))

		# Execute the relevant query (string-based, random, countryflag-based)
		if "random_amount" in query_parameters and query_parameters["random_amount"]:
			posts = self.execute_random_query(query_parameters)
		elif "country_flag" in query_parameters and query_parameters["country_flag"] != "all" and not query_parameters[
			"body_query"]:
			posts = self.execute_country_query(query_parameters)
		else:
			posts = self.execute_string_query(query_parameters)

		# Write posts to csv and update the DataBase status to finished
		if posts:
			self.dataset.update_status("Writing posts to result file")
			posts_to_csv(posts, results_file)
			self.dataset.update_status("Query finished, results are available.")
		elif posts is not None:
			self.dataset.update_status("Query finished, no results found.")

		num_posts = len(posts) if posts else 0

		# queue predefined post-processors
		if num_posts > 0 and query_parameters.get("next", []):
			for next in query_parameters.get("next"):
				next_parameters = next.get("parameters", {})
				next_type = next.get("type", "")
				available_processors = self.dataset.get_available_processors()

				# run it only if the post-processor is actually available for this query
				if next_type in available_processors:
					next_analysis = DataSet(parameters=next_parameters, type=next_type, db=self.db,
											parent=self.dataset.key,
											extension=available_processors[next_type]["extension"])
					self.queue.add_job(next_type, remote_id=next_analysis.key)

		# see if we need to register the result somewhere
		if query_parameters.get("copy_to", None):
			# copy the results to an arbitrary place that was passed
			if self.dataset.get_results_path().exists():
				# but only if we actually have something to copy
				shutil.copyfile(str(self.dataset.get_results_path()), query_parameters.get("copy_to"))
			else:
				# if copy_to was passed, that means it's important that this
				# file exists somewhere, so we create it as an empty file
				with open(query_parameters.get("copy_to"), "w") as empty_file:
					empty_file.write("")

		try:
			self.sphinx.close()
		except Error:
			# already closed earlier
			pass

		self.dataset.finish(num_rows=num_posts)

	def execute_string_query(self, query):
		"""
		Execute a query; get post data for given parameters

		This handles general search - anything that does not involve dense
		threads (those are handled by get_dense_threads()). First, Sphinx is
		queries with the search parameters to get the relevant post IDs; then
		the PostgreSQL is queried to return all posts for the found IDs, as
		well as (optionally) all other posts in the threads those posts were in.

		:param dict query:  Query parameters, as part of the DataSet object
		:return list:  Posts, sorted by thread and post ID, in ascending order
		"""

		# first, build the sphinx query
		where = []
		replacements = []
		match = []

		if query["min_date"]:
			where.append("timestamp >= %s")
			replacements.append(query["min_date"])

		if query["max_date"]:
			where.append("timestamp <= %s")
			replacements.append(query["max_date"])

		if query["board"] and query["board"] != "*":
			where.append("board = %s")
			replacements.append(query["board"])

		# escape / since it's a special character for Sphinx
		if query["body_query"]:
			match.append("@body " + self.escape_for_sphinx(query["body_query"]))

		if query["subject_query"]:
			match.append("@subject " + self.escape_for_sphinx(query["subject_query"]))

		# both possible FTS parameters go in one MATCH() operation
		if match:
			where.append("MATCH(%s)")
			replacements.append(" ".join(match))

		# query Sphinx
		self.dataset.update_status("Searching for matches")
		sphinx_start = time.time()
		where = " AND ".join(where)

		try:
			posts = self.fetch_sphinx(where, replacements)
			self.log.info("Sphinx query finished in %i seconds, %i results." % (time.time() - sphinx_start, len(posts)))
			self.dataset.update_status("Found %i matches. Collecting post data" % len(posts))
		except OperationalError:
			self.dataset.update_status(
				"Your query timed out. This is likely because it matches too many posts. Try again with a narrower date range or a more specific search query.")
			self.log.info("Sphinx query (body: %s/subject: %s) timed out after %i seconds" % (
				query["body_query"], query["subject_query"], time.time() - sphinx_start))
			self.sphinx.close()
			return None
		except ProgrammingError as e:
			self.dataset.update_status("Error during query. 4CAT admins have been notified; try again later.")
			self.log.error("Sphinx crash during query %s: %s" % (self.dataset.key, e))
			self.sphinx.close()
			return None

		self.sphinx.close()

		if not posts:
			# no results
			self.dataset.update_status("Query finished, but no results were found.")
			return None

		# query posts database
		postgres_start = time.time()
		self.log.info("Running full posts query")
		columns = ", ".join(self.return_cols)

		if not query["full_thread"] and not query["dense_threads"]:
			# just the exact post IDs we found via Sphinx
			post_ids = tuple([post["post_id"] for post in posts])

			# If the string posts should be filtered on a country
			if "country_flag" in query:
				if query["country_flag"] != "all":
					post_ids = self.filter_on_country(query, post_ids)

					# no results after country filtering
					if not post_ids:
						self.dataset.update_status("Query finished, but no results were found.")
						return None

			posts = self.fetch_posts(post_ids)

			self.dataset.update_status("Post data collected")
			self.log.info("Full posts query finished in %i seconds." % (time.time() - postgres_start))

		else:
			# all posts for all thread IDs found by Sphinx
			thread_ids = tuple([post["thread_id"] for post in posts])

			# if indicated, get dense thread ids
			if query["dense_threads"] and query["body_query"]:
				self.dataset.update_status("Post data collected. Filtering dense threads")
				thread_ids = self.filter_dense(thread_ids, query["body_query"], query["dense_percentage"],
											   query["dense_length"])

				# When there are no dense threads
				if not thread_ids:
					return []

			if len(posts) > 25000:
				self.log.info("Aborting full-thread query - too many OPs found")
				self.dataset.update_status(
					"Your query returned %i posts to fetch full thread data for - 4CAT cannot handle fetching full thread data for more than 25000 threads. Consider whether you really need full thread data, and if so, consider splitting your query into smaller periods of time or using a more precise query." % len(
						posts))
				self.job.finish()
				return None

			posts = self.fetch_threads(thread_ids)

			self.dataset.update_status("Post data collected")

			self.log.info("Full posts query finished in %i seconds." % (time.time() - postgres_start))

		return posts

	def execute_random_query(self, query):
		"""
		Execute a query; get post data for given parameters

		This handles general search - anything that does not involve dense
		threads (those are handled by get_dense_threads()). First, Sphinx is
		queries with the search parameters to get the relevant post IDs; then
		the PostgreSQL is queried to return all posts for the found IDs, as
		well as (optionally) all other posts in the threads those posts were in.

		:param dict query:  Query parameters, as part of the DataSet object
		:return list:  Posts, sorted by thread and post ID, in ascending order
		"""

		self.dataset.update_status("Fetching random posts")

		# Build random id query
		where = []
		replacements = []

		# Amount of random posts to get
		random_amount = query["random_amount"]

		# Get random post ids
		# `if max_date > 0` prevents postgres issues with big ints
		# INNER JOIN with threads table to lookup the board of the post
		if query["max_date"] > 0:
			post_ids = self.db.fetchall(
				"SELECT posts_" + self.prefix + ".id FROM posts_" + self.prefix + " INNER JOIN threads_" + self.prefix + " ON threads_" + self.prefix + ".id = posts_" + self.prefix + ".thread_id WHERE board = %s AND posts_" + self.prefix + ".timestamp >= %s AND posts_" + self.prefix + ".timestamp <= %s ORDER BY random() LIMIT %s;",
				(query["board"], query["min_date"], query["max_date"], random_amount,))
		else:
			post_ids = self.db.fetchall(
				"SELECT posts_" + self.prefix + ".id FROM posts_" + self.prefix + " INNER JOIN threads_" + self.prefix + " ON threads_" + self.prefix + ".id = posts_" + self.prefix + ".thread_id WHERE board = %s AND posts_" + self.prefix + ".timestamp >= %s ORDER BY random() LIMIT %s;",
				(query["board"], query["min_date"], random_amount,))

		# Fetch the posts
		post_ids = tuple([post["id"] for post in post_ids])
		posts = self.fetch_posts(post_ids)
		self.dataset.update_status("Post data collected")

		return posts

	def filter_on_country(self, query, post_ids):
		"""
		Filters retreived posts on whether they contain a country flag.

		:param dict query:  Query parameters, as part of the DataSet object
		:param list post_ids: List of post IDs to filter on
		:return tuple: filtered list of post ids
		"""

		country_flag = query["country_flag"]
		self.dataset.update_status("Filtering on country-specific posts")

		if country_flag == "europe":
			# country codes that can be selected in the web tool that are in
			# Europe (as defined by geographic location, using the Caucasus
			# mountains as a border)
			operator = "IN"
			country_flag = (
				"GB", "DE", "NL", "RU", "FI", "FR", "RO", "PL", "SE", "NO", "ES", "IE", "IT", "SI", "RS", "DK", "HR",
				"GR",
				"BG", "BE", "AT", "HU", "CH", "PT", "LT", "CZ", "EE", "UY", "LV", "SK", "MK", "UA", "IS", "BA", "CY",
				"GE",
				"LU", "ME", "AL", "MD", "IM", "EU", "BY", "MC", "AX", "KZ", "AM", "GG", "JE", "MT", "FO", "AZ", "LI",
				"AD")
		else:
			operator = "="

		posts = self.db.fetchall(
			"SELECT id FROM posts_" + self.prefix + " WHERE id IN %s AND country_code " + operator + " %s;",
			(post_ids, country_flag,))

		post_ids = tuple([post["id"] for post in posts])

		return post_ids

	def execute_country_query(self, query):
		"""
		Get posts based on their country flag

		:param str country: Country to filter on
		:return list: filtered list of post ids
		"""

		country_flag = query["country_flag"]

		# `if max_date > 0` prevents postgres issues with big ints
		self.dataset.update_status("Querying database for country-specific posts")

		if country_flag == "europe":
			# country codes that can be selected in the web tool that are in
			# Europe (as defined by geographic location, using the Caucasus
			# mountains as a border)
			operator = "IN"
			country_flag = (
				"GB", "DE", "NL", "RU", "FI", "FR", "RO", "PL", "SE", "NO", "ES", "IE", "IT", "SI", "RS", "DK", "HR",
				"GR",
				"BG", "BE", "AT", "HU", "CH", "PT", "LT", "CZ", "EE", "UY", "LV", "SK", "MK", "UA", "IS", "BA", "CY",
				"GE",
				"LU", "ME", "AL", "MD", "IM", "EU", "BY", "MC", "AX", "KZ", "AM", "GG", "JE", "MT", "FO", "AZ", "LI",
				"AD")

		else:
			operator = "="

		# if we just need the posts, we only need one query: else, first query
		# thread and post IDs and use those to filter the exact posts we need
		# which is done later after we check if we've actually found any posts
		# to begin with
		if query["dense_country_percentage"]:
			columns = "thread_id, id"
		else:
			columns = ", ".join(self.return_cols)

		# initial queries
		if query["max_date"] > 0:
			posts = self.db.fetchall(
				"SELECT " + columns + " FROM posts_" + self.prefix + " WHERE timestamp >= %s AND timestamp <= %s AND country_code " + operator + " %s;",
				(query["min_date"], query["max_date"], country_flag,))
		else:
			posts = self.db.fetchall(
				"SELECT " + columns + " FROM posts_" + self.prefix + " WHERE timestamp >= %s AND country_code " + operator + " %s;",
				(query["min_date"], country_flag,))

		# Return empty list if there's no matches
		if not posts:
			return []

		# Fetch all the posts
		if query["dense_country_percentage"]:
			# Get the full threads with country density
			self.dataset.update_status("Post data collected. Filtering dense threads")
			thread_ids = [post["thread_id"] for post in posts]
			thread_ids = self.filter_dense_country(thread_ids, country_flag, query["dense_country_percentage"])
			# Return empty list if there's no matches
			if not thread_ids:
				return []

			posts = self.fetch_threads(thread_ids)

		# done
		self.dataset.update_status("Post data collected. %i country-specific posts found." % len(posts))
		return posts

	def filter_dense(self, thread_ids, keyword, percentage, length):
		"""
		Filter posts for dense threads.
		Dense threads are threads that contain a keyword more than
		a given amount of times. This takes a post array as returned by
		`execute_string_query()` and filters it so that only posts in threads in which
		the keyword appears more than a given threshold's amount of times
		remain.

		:param list thread_ids:  Threads to filter, result of `execute_string_query()`
		:param string keyword:  Keyword that posts will be matched against
		:param float percentage:  How many posts in the thread need to qualify
		:param int length:  How long a thread needs to be to qualify
		:return list:  Filtered list of posts
		"""

		# for each thread, save number of posts and number of matching posts
		self.log.info("Filtering %s-dense threads from %i threads..." % (keyword, len(thread_ids)))

		keyword_posts = Counter(thread_ids)

		thread_ids = tuple([str(thread_id) for thread_id in thread_ids])
		total_posts = self.db.fetchall(
			"SELECT id, num_replies FROM threads_" + self.prefix + " WHERE id IN %s GROUP BY id", (thread_ids,))

		# Check wether the total posts / posts with keywords is longer than the given percentage,
		# and if the length is above the given threshold
		qualified_threads = []
		for total_post in total_posts:
			# Check if the length meets the threshold
			if total_post["num_replies"] >= length:
				# Check if the keyword density meets the threshold
				thread_density = float(keyword_posts[total_post["id"]] / total_post["num_replies"] * 100)
				if thread_density >= float(percentage):
					qualified_threads.append(total_post["id"])

		self.log.info("Dense thread filtering finished, %i threads left." % len(qualified_threads))
		filtered_threads = tuple([thread for thread in qualified_threads])
		return filtered_threads

	def filter_dense_country(self, thread_ids, country, percentage):
		"""
		Filter posts for dense country threads.
		Dense country threads are threads that contain a country flag more than
        a given amount of times. This takes a post array as returned by
        `execute_string_query()` and filters it so that only posts in threads in which
        the country flag appears more than a given threshold's amount of times
        remain.

		:param list thread_ids:  Threads to filter, result of `execute_country_query()`
		:param string country: Country that posts will be matched against
		:param float percentage:  How many posts in the thread need to qualify
		:return list:  Filtered list of posts
		"""

		# For each thread, save number of posts and number of matching posts
		self.log.info("Filtering %s-dense threads from %i threads..." % (country, len(thread_ids)))

		country_posts = Counter(thread_ids)

		thread_ids = tuple([str(thread_id) for thread_id in thread_ids])
		total_posts = self.db.fetchall(
			"SELECT id, num_replies FROM threads_" + self.prefix + " WHERE id IN %s GROUP BY id", (thread_ids,))

		# Check wether the total posts / posts with country flag is longer than the given percentage,
		# and if the length is above the given threshold
		qualified_threads = []
		for total_post in total_posts:
			# Check if the keyword density meets the threshold
			thread_density = float(country_posts[total_post["id"]] / total_post["num_replies"] * 100)
			if thread_density >= float(percentage):
				qualified_threads.append(total_post["id"])

		# Return thread IDs
		self.log.info("Dense thread filtering finished, %i threads left." % len(qualified_threads))
		filtered_threads = tuple([thread for thread in qualified_threads])
		return filtered_threads

	def escape_for_sphinx(self, string):
		"""
		SphinxQL has a couple of special characters that should be escaped if
		they are part of a query, but no native function is available to
		provide this functionality. This method does.

		Thanks: https://stackoverflow.com/a/6288301

		:param str string:  String to escape
		:return str: Escaped string
		"""
		return string.replace("/", "\\/")

	@abstractmethod
	def fetch_posts(self, post_ids):
		pass

	@abstractmethod
	def fetch_threads(self, thread_ids):
		pass

	@abstractmethod
	def fetch_sphinx(self, where, replacements):
		pass