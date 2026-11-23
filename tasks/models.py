from django.db import models


class Course(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Task(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=30, default="Tugas")  # Tugas, Kuis, UTS, UAS, Proyek
    weight = models.FloatField(default=0)  # bobot % dari BRP
    deadline = models.DateTimeField()
    est_hours = models.FloatField(null=True, blank=True)  # diisi engine analytics
    done = models.BooleanField(default=False)

    class Meta:
        ordering = ["deadline"]

    def __str__(self):
        return self.title


class Checkpoint(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="checkpoints")
    title = models.CharField(max_length=200)
    due_date = models.DateField()
    done = models.BooleanField(default=False)

    class Meta:
        ordering = ["due_date"]

    def __str__(self):
        return self.title
