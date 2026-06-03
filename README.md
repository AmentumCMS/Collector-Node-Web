# What is this?

[![Release](https://github.com/amentumcms/Collector-Node-Web/actions/workflows/yarn.yml/badge.svg?branch=main)](https://github.com/amentumcms/Collector-Node-Web/actions/workflows/yarn.yml)

This is a project that automatically collects artifacts to ease in air-gapped transfer from the internet.

It runs on every push (all branches) and on a monthly schedule (1st of the month at midnight UTC). On the main branch it creates a GitHub release; on other branches the pipeline runs for validation only.

In this case, it collects node_modules and .yarn/cache needed.
