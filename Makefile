.PHONY: install test eval lint demo web clean

install:
	pip install -r requirements.txt
	pip install -e .

test:
	python3 -m unittest discover -s tests -p "test_*.py" -v

eval:
	python3 eval/run_eval.py

lint:
	ruff check session_publisher_agent tests eval app.py cli.py

demo:
	python3 cli.py --demo

web:
	python3 app.py --port 8080

clean:
	rm -rf __pycache__ .pytest_cache .ruff_cache .agent_data eval_report.json
