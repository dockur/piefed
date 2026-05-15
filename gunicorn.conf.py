import multiprocessing

cpu_cores = multiprocessing.cpu_count()

workers = max(4, cpu_cores // 2)
threads = 2

worker_tmp_dir = '/dev/shm'

bind = '0.0.0.0:5000'
umask = 0o007
reload = False

worker_class = 'gthread'

#logging
accesslog = '-'
errorlog = '-'

max_requests = 2000
max_requests_jitter = 50
