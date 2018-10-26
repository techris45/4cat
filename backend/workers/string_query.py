import time
import config
import csv
import os
import pickle as p

from backend.lib.database import Database
from backend.lib.logger import Logger
from backend.lib.query import SearchQuery
from backend.lib.queue import JobClaimedException
from backend.lib.helpers import get_absolute_folder
from backend.lib.worker import BasicWorker

from bs4 import BeautifulSoup


class stringQuery(BasicWorker):
	"""
	Process substring queries from the front-end
	Requests are added to the pool as "query" jobs

	E.g. queue.addJob(type="query", details={"str_query": "skyrim", "col_query": "body_vector"})
	"""

	type = "query"
	pause = 2
	max_workers = 3

	def __init__(self, logger, manager):
		"""
		Set up database connection - we need one to perform the query
		"""
		super().__init__(logger=logger, manager=manager)

	def work(self):

		job = self.queue.get_job(jobtype="query")

		if not job:
			self.log.debug("No string queries, sleeping for 10 seconds")
			time.sleep(10)

		else:
			try:
				self.queue.claim_job(job)
			except JobClaimedException:
				return

			self.log.info("Executing string query")
			
			log = Logger()
			db = Database(logger=log)
			query = SearchQuery(key=job["remote_id"], db=db)

			# get query details
			di_query_parameters = query.get_parameters()
			col = di_query_parameters["col_query"]
			str_query = di_query_parameters["str_query"]

			resultsfile = query.get_results_path() 

			self.log.info(resultsfile)

			# check whether timestamp parameters are provided
			if "min_date" in di_query_parameters and "max_date" in di_query_parameters:
				min_date = di_query_parameters["min_date"]
				min_date = di_query_parameters["max_date"]

			# execute the query on the relevant column
			di_matches = self.execute_query(str(col), str(str_query))

			# write to csv if there substring matches. Else set query as empty
			if di_matches:
				self.dict_to_csv(di_matches, resultsfile)
			else:
				query.set_empty()

			# done!
			query.finish()
			self.queue.finish_job(job)

		looping = False

	def execute_query(self, body_query, subject_query, full_thread, min_date=None, max_date=None):
		"""
		Query the relevant column of the chan data.
		Converts parameters to SQL statements.

		:param	body_query		str,	Query string for post body
		:param	subject_query	str,	Query string for post subject
		:param	full_query		bool,	Whether data from the full thread should be returned.
										Only works when subject is queried.  
		:param	min_timestamp	str,	Min timestamps to search for
		:param	max_timestamp	str,	Max timestamps to search for
		
		"""

		# Set SQL statements depending on parameters provided by user
		replacements = []
		sql_post = ''
		sql_subject = ''
		sql_min_date = ''
		sql_max_date = ''
		sql_log = 'Starting substring query where '

		if body_query:
			sql_post = " AND body_vector @@ to_tsquery(" + body_query + ")"
			replacements.append(sql_post)
			sql_log = sql_log + "'" + body_query + "' is in body, "
		if subject_query:
			sql_subject = " AND subject_vector @@ to_tsquery(" + subject_query + ")"
			replacements.append(sql_subject)
			sql_log = sql_log + "'" + subject_query + "' is in subject, "
		if min_date != 0:
			sql_min_date = " AND timestamp > " + str(min_date)
			replacements.append(sql_min_date)
			sql_log = sql_log + "is posted after " + str(min_date) ", "
		if max_date != 0:
			sql_max_date = " AND timestamp < " + str(max_date)
			replacements.append(sql_max_date)
			sql_log = sql_log + "is posted before " + str(max_date) ", "

		sql_log = sql_log[2:] + '.'
		self.log.info(sql_log)

		# Some timekeeping
		start_time = time.time()

		# Fetch only posts
		if full_thread == False:
			try:
				di_matches = self.db.fetchall("SELECT * FROM posts WHERE true" + sql_post + sql_subject + sql_min_date + sql_max_date, replacements)
			except Exception as error:
				return str(error)

		# Fetch full thread data
		elif full_thread and body_query:
			try:
				li_thread_ids = self.db.fetchall("SELECT thread_id FROM posts WHERE true" + sql_post + sql_subject + sql_min_date + sql_max_date, replacements)
			except Exception as error:
				return str(error)
			try:
				di_matches = self.db.fetchall("SELECT * FROM posts WHERE thread_id IN %s", (li_thread_ids))
			except Exception as error:
				return str(error)
		else:
			self.log.warning("Not enough parameters provided for substring query.")
			return -1

		self.log.info("Finished query in " + str(round((time.time() - start_time), 4)) + " seconds")

		return di_matches

	def dict_to_csv(self, di_input, filepath, clean_csv=True):
		"""
		Takes a dictionary of results, converts it to a csv, and writes it to the data folder.
		The respective csvs will be available to the user.

		:param di_input:    dict derived with db.fetchall(), used as input
		:param filename:    filename for the resulting csv
		:param clean_csv:   whether to parse the raw HTML data to clean text. If True (default), writing takes 1.5 times longer.

		"""
		#self.log.info(type(di_input))
		# some error handling
		if type(di_input) != list:
			self.log.error('Please use a dict object instead of ' +  str(type(di_input)) + 'to convert to csv')
			return -1
		if filepath == '':
			self.log.error('No file path for results file provided')
			return -1

		#filepath = get_absolute_folder(config.PATH_DATA) + "/" + filename + '.csv'

		# fields to save in the offered csv (tweak later)
		fieldnames = ['id', 'timestamp', 'body', 'subject']
		print('filepath: ',filepath)
		# write the dictionary to a csv
		with open(filepath, 'w', encoding='utf-8') as csvfile:
			writer = csv.DictWriter(csvfile, fieldnames=fieldnames, lineterminator='\n')
			writer.writeheader()

			if clean_csv:
				# Parsing: remove the HTML tags, but keep the <br> as a newline
				# Takes around 1.5 times longer
				for post in di_input:
					post['body'] = post['body'].replace('<br>', '\n')
					post["body"] = BeautifulSoup(post["body"], 'html.parser').get_text()
					writer.writerow(post)
			else:
				writer.writerows(li_input)

		return filepath